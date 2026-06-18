"""CLI entry points for the installed package."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import uvicorn

from api.admin_urls import local_admin_url, local_proxy_root_url
from api.app import GracefulLifespanApp, create_app
from cli.adapters.base import ClientCliAdapter
from cli.adapters.claude import CLAUDE_CLI_ADAPTER
from cli.adapters.codex import CODEX_CLI_ADAPTER
from cli.codex_model_catalog import (
    build_codex_model_catalog,
    write_codex_model_catalog,
)
from cli.process_registry import (
    kill_all_best_effort,
    kill_pid_tree_best_effort,
    register_pid,
    unregister_pid,
)
from config.paths import (
    codex_model_catalog_path,
    config_dir_path,
    legacy_env_paths,
    managed_env_path,
)
from config.settings import Settings, get_settings

PROXY_PREFLIGHT_PATH = "/health"
PROXY_PREFLIGHT_TIMEOUT_SECONDS = 1.5
SERVER_GRACEFUL_SHUTDOWN_SECONDS = 5
FREE_CODEX_STARTUP_TIMEOUT_SECONDS = 30.0
CODEX_CONFIG_DIRNAME = ".codex"
CODEX_CONFIG_FILENAME = "config.toml"
CODEX_AUTH_FILENAME = "auth.json"
CODEX_WRAPPER_STATE_FILENAME = "codex-wrapper-state.json"
CODEX_WRAPPER_CONFIG_BACKUP_FILENAME = "codex-config.backup.toml"
CODEX_WRAPPER_AUTH_BACKUP_FILENAME = "codex-auth.backup.json"


def _load_env_template() -> str:
    """Load the canonical root env template from package resources or source."""
    import importlib.resources

    packaged = importlib.resources.files("cli").joinpath("env.example")
    if packaged.is_file():
        return packaged.read_text("utf-8")

    source_template = Path(__file__).resolve().parents[1] / ".env.example"
    if source_template.is_file():
        return source_template.read_text(encoding="utf-8")

    raise FileNotFoundError("Could not find bundled or source .env.example template.")


def serve(argv: Sequence[str] | None = None) -> None:
    """Start the FastAPI server (registered as `fcc-server` script)."""
    args = list(sys.argv[1:] if argv is None else argv)
    browser_launch_enabled = True
    if args == ["--nl"]:
        browser_launch_enabled = False
    elif args:
        print("Usage: fcc-server [--nl]", file=sys.stderr)
        raise SystemExit(2)

    opened_admin_browser = False
    try:
        try:
            while True:
                _migrate_legacy_env_if_missing()
                settings = get_settings()
                if not _run_supervised_server(
                    settings,
                    open_admin_browser=browser_launch_enabled
                    and not opened_admin_browser,
                ):
                    return
                opened_admin_browser = True
                get_settings.cache_clear()
        except KeyboardInterrupt:
            return
    finally:
        kill_all_best_effort()


def free_codex(argv: Sequence[str] | None = None) -> None:
    """Run the local FCC server and temporarily point Codex user config at it."""

    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--sleep"]:
        restored = _restore_standard_codex_config()
        if restored:
            print("Restored standard Codex configuration.")
        else:
            print("Codex configuration already uses the standard setup.")
        return
    if args:
        print("Usage: free-codex [--sleep]", file=sys.stderr)
        raise SystemExit(2)

    settings = get_settings()
    proxy_root_url = local_proxy_root_url(settings)
    _restore_standard_codex_config()
    _activate_codex_proxy_config(settings)

    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(["uv", "run", "fcc-server"], cwd=_repo_root_path())
        if process.pid:
            register_pid(process.pid)
        if not _wait_for_proxy_ready(proxy_root_url, process):
            print(
                "Free Claude Code proxy did not become reachable. "
                "Restored the standard Codex configuration.",
                file=sys.stderr,
            )
            if process.poll() is None and process.pid:
                kill_pid_tree_best_effort(process.pid)
                process.wait()
            raise SystemExit(process.returncode or 1)

        catalog_path = _prepare_codex_model_catalog(proxy_root_url, settings)
        if catalog_path is not None:
            _activate_codex_proxy_config(settings, catalog_path=catalog_path)

        return_code = process.wait()
    except KeyboardInterrupt:
        if process is not None and process.pid:
            kill_pid_tree_best_effort(process.pid)
            process.wait()
        raise
    finally:
        if process is not None and process.pid:
            unregister_pid(process.pid)
        _restore_standard_codex_config()

    raise SystemExit(return_code)


def _admin_browser_open_enabled() -> bool:
    """Whether to open /admin when the server becomes reachable (FCC_OPEN_BROWSER)."""

    raw = os.environ.get("FCC_OPEN_BROWSER", "true").strip().lower()
    return raw not in {"", "0", "false", "no"}


def _schedule_open_admin_browser(settings: Settings) -> None:
    """After /health succeeds, open the admin UI in the default browser (daemon thread)."""

    if not _admin_browser_open_enabled():
        return

    admin_url = local_admin_url(settings)
    proxy_root_url = local_proxy_root_url(settings)

    def open_when_ready() -> None:
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if _preflight_proxy(proxy_root_url) is None:
                webbrowser.open(admin_url)
                return
            time.sleep(0.15)

    threading.Thread(
        target=open_when_ready, name="fcc-open-admin-browser", daemon=True
    ).start()


def _run_supervised_server(settings: Settings, *, open_admin_browser: bool) -> bool:
    """Run one uvicorn server instance; return whether admin requested restart."""

    restart_requested = False
    server_holder: dict[str, uvicorn.Server] = {}

    def request_restart() -> None:
        nonlocal restart_requested
        restart_requested = True
        if server := server_holder.get("server"):
            server.should_exit = True

    app = create_app(lifespan_enabled=False)
    app.state.admin_restart_callback = request_restart
    asgi_app = GracefulLifespanApp(app)
    config = uvicorn.Config(
        asgi_app,
        host=settings.host,
        port=settings.port,
        log_level="debug",
        timeout_graceful_shutdown=SERVER_GRACEFUL_SHUTDOWN_SECONDS,
    )
    server = uvicorn.Server(config)
    server_holder["server"] = server
    if open_admin_browser:
        _schedule_open_admin_browser(settings)
    server.run()
    return restart_requested


def _repo_root_path() -> str:
    """Return the source checkout root used to launch uv-backed entrypoints."""

    return str(Path(__file__).resolve().parents[1])


def _codex_dir_path() -> Path:
    """Return the user Codex configuration directory."""

    return Path.home() / CODEX_CONFIG_DIRNAME


def _codex_config_path() -> Path:
    """Return the user Codex config path."""

    return _codex_dir_path() / CODEX_CONFIG_FILENAME


def _codex_auth_path() -> Path:
    """Return the user Codex auth path."""

    return _codex_dir_path() / CODEX_AUTH_FILENAME


def _codex_wrapper_state_path() -> Path:
    """Return the wrapper state file stored under FCC config."""

    return config_dir_path() / CODEX_WRAPPER_STATE_FILENAME


def _codex_wrapper_config_backup_path() -> Path:
    """Return the saved backup of the user's Codex config file."""

    return config_dir_path() / CODEX_WRAPPER_CONFIG_BACKUP_FILENAME


def _codex_wrapper_auth_backup_path() -> Path:
    """Return the saved backup of the user's Codex auth file."""

    return config_dir_path() / CODEX_WRAPPER_AUTH_BACKUP_FILENAME


def _activate_codex_proxy_config(
    settings: Settings, *, catalog_path: Path | None = None
) -> None:
    """Persist a temporary Codex config that points the real Codex app at FCC."""

    codex_dir = _codex_dir_path()
    codex_dir.mkdir(parents=True, exist_ok=True)
    config_dir_path().mkdir(parents=True, exist_ok=True)
    _ensure_codex_wrapper_backup_state()

    config_path = _codex_config_path()
    auth_path = _codex_auth_path()
    existing_config = ""
    if config_path.exists():
        existing_config = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        _merge_codex_proxy_config(existing_config, settings, catalog_path=catalog_path),
        encoding="utf-8",
    )
    auth_path.write_text(
        json.dumps(_render_codex_auth_payload(settings, auth_path), indent=2) + "\n",
        encoding="utf-8",
    )


def _ensure_codex_wrapper_backup_state() -> None:
    """Capture the user's current Codex config once so it can be restored later."""

    state_path = _codex_wrapper_state_path()
    if state_path.exists():
        return

    config_path = _codex_config_path()
    auth_path = _codex_auth_path()
    config_backup = _codex_wrapper_config_backup_path()
    auth_backup = _codex_wrapper_auth_backup_path()

    state = {
        "config_existed": config_path.exists(),
        "auth_existed": auth_path.exists(),
        "config_restore": _capture_codex_config_restore_state(
            config_path.read_text(encoding="utf-8") if config_path.exists() else ""
        ),
        "auth_restore": _capture_codex_auth_restore_state(
            auth_path.read_text(encoding="utf-8") if auth_path.exists() else ""
        ),
    }
    if config_path.exists():
        config_backup.write_text(
            config_path.read_text(encoding="utf-8"), encoding="utf-8"
        )
    if auth_path.exists():
        auth_backup.write_text(auth_path.read_text(encoding="utf-8"), encoding="utf-8")
    state_path.write_text(json.dumps(state), encoding="utf-8")


def _merge_codex_proxy_config(
    existing_config: str, settings: Settings, *, catalog_path: Path | None = None
) -> str:
    """Merge FCC settings into the user's Codex config without erasing other content."""

    config_text = existing_config
    config_text = _upsert_root_toml_key(
        config_text, "model_provider", json.dumps("fcc")
    )
    if catalog_path is None:
        config_text = _remove_root_toml_key(config_text, "model_catalog_json")
    else:
        config_text = _upsert_root_toml_key(
            config_text, "model_catalog_json", json.dumps(str(catalog_path))
        )

    config_text = _upsert_toml_table(
        config_text,
        "model_providers.fcc",
        {
            "name": json.dumps("Free Claude Code"),
            "base_url": json.dumps(f"{local_proxy_root_url(settings).rstrip('/')}/v1"),
            "env_key": json.dumps("FCC_CODEX_API_KEY"),
            "wire_api": json.dumps("responses"),
        },
    )
    config_text = _upsert_toml_table(
        config_text,
        "shell_environment_policy.set",
        {
            "FCC_CODEX_API_KEY": json.dumps(
                settings.anthropic_auth_token.strip() or "fcc-no-auth"
            )
        },
    )
    return config_text.rstrip() + "\n"


def _render_codex_proxy_config(
    settings: Settings, *, catalog_path: Path | None = None
) -> str:
    """Render a temporary Codex config.toml for the FCC provider."""

    lines = [f"model_provider = {json.dumps('fcc')}"]
    if catalog_path is not None:
        lines.append(f"model_catalog_json = {json.dumps(str(catalog_path))}")
    lines.extend(
        [
            "",
            "[model_providers.fcc]",
            f"name = {json.dumps('Free Claude Code')}",
            f"base_url = {json.dumps(f'{local_proxy_root_url(settings).rstrip("/")}/v1')}",
            f"env_key = {json.dumps('FCC_CODEX_API_KEY')}",
            f"wire_api = {json.dumps('responses')}",
            "",
        ]
    )
    return "\n".join(lines)


def _capture_codex_config_restore_state(config_text: str) -> dict[str, str | None]:
    """Capture only the config surfaces FCC mutates so restore can be non-destructive."""

    return {
        "model_provider": _extract_root_toml_key(config_text, "model_provider"),
        "model_catalog_json": _extract_root_toml_key(config_text, "model_catalog_json"),
        "fcc_provider_table": _extract_toml_table_block(
            config_text, "model_providers.fcc"
        ),
        "shell_env_fcc_api_key": _extract_toml_table_key(
            config_text, "shell_environment_policy.set", "FCC_CODEX_API_KEY"
        ),
    }


def _capture_codex_auth_restore_state(auth_text: str) -> dict[str, object]:
    """Capture only the auth.json key FCC mutates so restore can be non-destructive."""

    try:
        payload = json.loads(auth_text) if auth_text else {}
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return {
        "fcc_codex_api_key_present": "FCC_CODEX_API_KEY" in payload,
        "fcc_codex_api_key": payload.get("FCC_CODEX_API_KEY"),
    }


def _extract_root_toml_key(config_text: str, key: str) -> str | None:
    """Return the raw TOML value for a root-level key, if present."""

    insertion_point = _root_toml_insertion_point(config_text)
    root_section = config_text[:insertion_point]
    match = re.search(rf"(?m)^{re.escape(key)}\s*=\s*(?P<value>.+)$", root_section)
    return match.group("value") if match else None


def _upsert_root_toml_key(config_text: str, key: str, value: str) -> str:
    """Set or add a root-level TOML key while preserving the rest of the config."""

    insertion_point = _root_toml_insertion_point(config_text)
    root_section = config_text[:insertion_point]
    remainder = config_text[insertion_point:]
    pattern = re.compile(rf"(?m)^{re.escape(key)}\s*=.*$")
    replacement = f"{key} = {value}"
    if pattern.search(root_section):
        return pattern.sub(replacement, root_section, count=1) + remainder

    prefix = root_section.rstrip("\n")
    suffix = remainder.lstrip("\n")
    inserted = replacement if not prefix else f"{prefix}\n{replacement}"
    if suffix:
        return f"{inserted}\n\n{suffix}"
    return f"{inserted}\n"


def _remove_root_toml_key(config_text: str, key: str) -> str:
    """Remove a root-level TOML key if present."""

    insertion_point = _root_toml_insertion_point(config_text)
    root_section = config_text[:insertion_point]
    remainder = config_text[insertion_point:]
    pattern = re.compile(rf"(?m)^{re.escape(key)}\s*=.*$\n?")
    return pattern.sub("", root_section, count=1) + remainder


def _root_toml_insertion_point(config_text: str) -> int:
    """Return where root-level keys should be inserted before the first table."""

    match = re.search(r"(?m)^\[", config_text)
    return match.start() if match else len(config_text)


def _extract_toml_table_block(config_text: str, table_name: str) -> str | None:
    """Return the full TOML table block, if present."""

    table_pattern = re.compile(rf"(?ms)^\[{re.escape(table_name)}\]\s*\n.*?(?=^\[|\Z)")
    match = table_pattern.search(config_text)
    return match.group(0).rstrip("\n") if match else None


def _remove_toml_table(config_text: str, table_name: str) -> str:
    """Remove a TOML table block if present."""

    table_pattern = re.compile(rf"(?ms)^\[{re.escape(table_name)}\]\s*\n.*?(?=^\[|\Z)")
    updated = table_pattern.sub("", config_text, count=1)
    return updated.lstrip("\n")


def _restore_toml_table_block(
    config_text: str, table_name: str, table_block: str | None
) -> str:
    """Restore one TOML table block to its captured value."""

    updated = _remove_toml_table(config_text, table_name)
    if table_block is None:
        return updated
    appended = [updated.rstrip("\n"), "", table_block.rstrip("\n")]
    return "\n".join(part for part in appended if part != "") + "\n"


def _upsert_toml_table(
    config_text: str, table_name: str, entries: Mapping[str, str]
) -> str:
    """Set or add key/value entries inside a TOML table."""

    header = f"[{table_name}]"
    table_pattern = re.compile(
        rf"(?ms)^\[{re.escape(table_name)}\]\s*\n(?P<body>.*?)(?=^\[|\Z)"
    )
    match = table_pattern.search(config_text)
    if match:
        body = match.group("body")
        updated_body = body
        for key, value in entries.items():
            key_pattern = re.compile(rf"(?m)^{re.escape(key)}\s*=.*$")
            line = f"{key} = {value}"
            if key_pattern.search(updated_body):
                updated_body = key_pattern.sub(line, updated_body, count=1)
            else:
                updated_body = updated_body.rstrip("\n")
                updated_body = (
                    f"{updated_body}\n{line}\n" if updated_body else f"{line}\n"
                )
        return (
            config_text[: match.start()]
            + header
            + "\n"
            + updated_body.rstrip("\n")
            + "\n"
            + config_text[match.end() :]
        )

    appended = [config_text.rstrip("\n"), "", header]
    appended.extend(f"{key} = {value}" for key, value in entries.items())
    return "\n".join(part for part in appended if part != "") + "\n"


def _extract_toml_table_key(config_text: str, table_name: str, key: str) -> str | None:
    """Return the raw TOML value for a key inside one table, if present."""

    table_block = _extract_toml_table_block(config_text, table_name)
    if table_block is None:
        return None
    match = re.search(rf"(?m)^{re.escape(key)}\s*=\s*(?P<value>.+)$", table_block)
    return match.group("value") if match else None


def _remove_toml_table_key(config_text: str, table_name: str, key: str) -> str:
    """Remove one key from a TOML table, deleting the table if it becomes empty."""

    table_pattern = re.compile(
        rf"(?ms)^\[{re.escape(table_name)}\]\s*\n(?P<body>.*?)(?=^\[|\Z)"
    )
    match = table_pattern.search(config_text)
    if match is None:
        return config_text

    body = re.sub(rf"(?m)^{re.escape(key)}\s*=.*$\n?", "", match.group("body"), count=1)
    body = body.strip("\n")
    if body:
        suffix = config_text[match.end() :]
        line_breaks = "\n\n" if suffix else "\n"
        replacement = f"[{table_name}]\n{body}{line_breaks}"
        return config_text[: match.start()] + replacement + config_text[match.end() :]
    return _remove_toml_table(config_text, table_name)


def _render_codex_auth_payload(
    settings: Settings, auth_path: Path
) -> dict[str, object]:
    """Return auth.json contents with the FCC auth token injected."""

    payload: dict[str, object] = {}
    if auth_path.exists():
        try:
            existing = json.loads(auth_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = None
        if isinstance(existing, dict):
            payload.update(existing)
    payload["FCC_CODEX_API_KEY"] = (
        settings.anthropic_auth_token.strip() or "fcc-no-auth"
    )
    return payload


def _restore_standard_codex_config() -> bool:
    """Restore the user's previous Codex config/auth files if the wrapper changed them."""

    state_path = _codex_wrapper_state_path()
    if not state_path.exists():
        return False

    state = json.loads(state_path.read_text(encoding="utf-8"))
    config_path = _codex_config_path()
    auth_path = _codex_auth_path()
    config_backup = _codex_wrapper_config_backup_path()
    auth_backup = _codex_wrapper_auth_backup_path()
    config_restore = state.get("config_restore")
    auth_restore = state.get("auth_restore")

    if isinstance(config_restore, dict):
        config_text = (
            config_path.read_text(encoding="utf-8") if config_path.exists() else ""
        )
        config_text = _restore_toml_table_block(
            config_text,
            "model_providers.fcc",
            _coerce_optional_string(config_restore.get("fcc_provider_table")),
        )
        config_text = _restore_toml_table_key(
            config_text,
            "shell_environment_policy.set",
            "FCC_CODEX_API_KEY",
            _coerce_optional_string(config_restore.get("shell_env_fcc_api_key")),
        )
        config_text = _restore_root_toml_key(
            config_text,
            "model_catalog_json",
            _coerce_optional_string(config_restore.get("model_catalog_json")),
        )
        config_text = _restore_root_toml_key(
            config_text,
            "model_provider",
            _coerce_optional_string(config_restore.get("model_provider")),
        )
        if config_text.strip():
            config_path.write_text(config_text.rstrip() + "\n", encoding="utf-8")
        elif config_path.exists():
            config_path.unlink()
    elif state.get("config_existed"):
        if not config_backup.exists():
            raise RuntimeError(
                "Missing Codex config backup; cannot restore config.toml."
            )
        config_path.write_text(
            config_backup.read_text(encoding="utf-8"), encoding="utf-8"
        )
    elif config_path.exists():
        config_path.unlink()

    if isinstance(auth_restore, dict):
        payload: dict[str, object] = {}
        if auth_path.exists():
            try:
                existing_payload = json.loads(auth_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                existing_payload = {}
            if isinstance(existing_payload, dict):
                payload.update(existing_payload)
        if auth_restore.get("fcc_codex_api_key_present"):
            payload["FCC_CODEX_API_KEY"] = auth_restore.get("fcc_codex_api_key")
        else:
            payload.pop("FCC_CODEX_API_KEY", None)
        if payload:
            auth_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        elif auth_path.exists():
            auth_path.unlink()
    elif state.get("auth_existed"):
        if not auth_backup.exists():
            raise RuntimeError("Missing Codex auth backup; cannot restore auth.json.")
        auth_path.write_text(auth_backup.read_text(encoding="utf-8"), encoding="utf-8")
    elif auth_path.exists():
        auth_path.unlink()

    if config_backup.exists():
        config_backup.unlink()
    if auth_backup.exists():
        auth_backup.unlink()
    state_path.unlink()
    return True


def _coerce_optional_string(value: object) -> str | None:
    """Return a string restore value or None when the snapshot omitted it."""

    return value if isinstance(value, str) else None


def _restore_root_toml_key(config_text: str, key: str, value: str | None) -> str:
    """Restore one root-level key to its captured value."""

    if value is None:
        return _remove_root_toml_key(config_text, key)
    return _upsert_root_toml_key(config_text, key, value)


def _restore_toml_table_key(
    config_text: str, table_name: str, key: str, value: str | None
) -> str:
    """Restore one TOML table key to its captured value."""

    if value is None:
        return _remove_toml_table_key(config_text, table_name, key)
    return _upsert_toml_table(config_text, table_name, {key: value})


def _wait_for_proxy_ready(
    proxy_root_url: str, process: subprocess.Popen[bytes]
) -> bool:
    """Wait for the launched FCC server to accept health checks."""

    deadline = time.monotonic() + FREE_CODEX_STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _preflight_proxy(proxy_root_url) is None:
            return True
        if process.poll() is not None:
            return False
        time.sleep(0.15)
    return False


def init() -> None:
    """Scaffold config at ~/.fcc/.env (registered as `fcc-init`)."""
    config_dir = config_dir_path()
    env_file = managed_env_path()

    migrated_from = _migrate_legacy_env_if_missing()
    if migrated_from is not None:
        print(f"Config migrated from {migrated_from} to {env_file}")
        print(
            "Edit it to set your API keys and model preferences, then run: fcc-server"
        )
        return

    if env_file.exists():
        print(f"Config already exists at {env_file}")
        print("Delete it first if you want to reset to defaults.")
        return

    config_dir.mkdir(parents=True, exist_ok=True)
    template = _load_env_template()
    env_file.write_text(template, encoding="utf-8")
    print(f"Config created at {env_file}")
    print("Edit it to set your API keys and model preferences, then run: fcc-server")


def _migrate_legacy_env_if_missing() -> Path | None:
    """Copy a legacy user env into the managed config path when absent."""

    env_file = managed_env_path()
    if env_file.exists():
        return None

    # TODO: Remove after the ~/.fcc/.env migration has had a release cycle.
    for legacy_env in legacy_env_paths():
        if not legacy_env.is_file():
            continue
        env_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(legacy_env, env_file)
        return legacy_env

    return None


def _claude_child_env(
    settings: Settings, base_env: Mapping[str, str]
) -> dict[str, str]:
    """Return a Claude Code environment that targets this proxy."""

    return CLAUDE_CLI_ADAPTER.build_launcher_env(
        proxy_root_url=local_proxy_root_url(settings),
        auth_token=settings.anthropic_auth_token,
        base_env=base_env,
    )


def _preflight_proxy(proxy_root_url: str) -> str | None:
    """Return an error message when the local proxy health check is unreachable."""

    url = f"{proxy_root_url.rstrip('/')}{PROXY_PREFLIGHT_PATH}"
    request = Request(url, method="GET")
    try:
        with urlopen(request, timeout=PROXY_PREFLIGHT_TIMEOUT_SECONDS) as response:
            status_code = response.getcode()
    except HTTPError as exc:
        return f"returned HTTP {exc.code}"
    except URLError as exc:
        return str(exc.reason)
    except OSError as exc:
        return str(exc)

    if not 200 <= status_code < 300:
        return f"returned HTTP {status_code}"
    return None


def launch_claude(argv: Sequence[str] | None = None) -> None:
    """Launch Claude Code with Free Claude Code proxy environment variables."""

    _launch_client_cli(CLAUDE_CLI_ADAPTER, argv)


def launch_codex(argv: Sequence[str] | None = None) -> None:
    """Launch Codex CLI with Free Claude Code proxy configuration."""

    _launch_client_cli(CODEX_CLI_ADAPTER, argv)


def _launch_client_cli(
    adapter: ClientCliAdapter, argv: Sequence[str] | None = None
) -> None:
    """Launch a client CLI with Free Claude Code proxy environment variables."""

    settings = get_settings()
    proxy_root_url = local_proxy_root_url(settings)
    if error := _preflight_proxy(proxy_root_url):
        print(
            f"Free Claude Code proxy is not reachable at {proxy_root_url}: {error}",
            file=sys.stderr,
        )
        print("Start it in another terminal with: fcc-server", file=sys.stderr)
        raise SystemExit(1)

    args = list(sys.argv[1:] if argv is None else argv)
    binary_name = adapter.get_launcher_binary_name(settings)
    client_command = shutil.which(binary_name)
    if client_command is None:
        print(
            f"Could not find {adapter.display_name} command: {binary_name}",
            file=sys.stderr,
        )
        print(adapter.install_hint, file=sys.stderr)
        raise SystemExit(127)

    command = adapter.build_launcher_command(
        binary_path=client_command,
        argv=args,
        settings=settings,
        proxy_root_url=proxy_root_url,
    )
    catalog_args = _codex_model_catalog_config_args(adapter, proxy_root_url, settings)
    if catalog_args:
        command = [command[0], *catalog_args, *command[1:]]
    env = adapter.build_launcher_env(
        proxy_root_url=proxy_root_url,
        auth_token=settings.anthropic_auth_token,
        base_env=os.environ,
    )
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(command, env=env)
        if process.pid:
            register_pid(process.pid)
        return_code = process.wait()
    except FileNotFoundError:
        print(
            f"Could not find {adapter.display_name} command: {binary_name}",
            file=sys.stderr,
        )
        print(adapter.install_hint, file=sys.stderr)
        raise SystemExit(127) from None
    except KeyboardInterrupt:
        if process is not None and process.pid:
            kill_pid_tree_best_effort(process.pid)
            process.wait()
        raise
    finally:
        if process is not None and process.pid:
            unregister_pid(process.pid)

    raise SystemExit(return_code)


def _codex_model_catalog_config_args(
    adapter: ClientCliAdapter, proxy_root_url: str, settings: Settings
) -> list[str]:
    if adapter.id != CODEX_CLI_ADAPTER.id:
        return []

    catalog_path = _prepare_codex_model_catalog(proxy_root_url, settings)
    if catalog_path is None:
        return []
    return CODEX_CLI_ADAPTER.build_model_catalog_config_args(str(catalog_path))


def _prepare_codex_model_catalog(
    proxy_root_url: str, settings: Settings
) -> Path | None:
    """Fetch and write a Codex model catalog from the FCC models route."""

    try:
        models_response = _fetch_proxy_models_response(
            proxy_root_url, settings.anthropic_auth_token
        )
        catalog = build_codex_model_catalog(models_response)
        models = catalog.get("models")
        if not isinstance(models, list) or not models:
            print(
                "Free Claude Code warning: Codex model catalog is empty; "
                "launching without model picker catalog.",
                file=sys.stderr,
            )
            return None
        catalog_path = codex_model_catalog_path()
        write_codex_model_catalog(catalog_path, catalog)
    except Exception as exc:
        print(
            "Free Claude Code warning: could not prepare Codex model catalog "
            f"({exc}); launching without model picker catalog.",
            file=sys.stderr,
        )
        return None

    return catalog_path


def _fetch_proxy_models_response(
    proxy_root_url: str, auth_token: str
) -> dict[str, object]:
    url = f"{proxy_root_url.rstrip('/')}/v1/models"
    headers: dict[str, str] = {}
    if token := auth_token.strip():
        headers["X-API-Key"] = token

    request = Request(url, headers=headers, method="GET")
    with urlopen(request, timeout=PROXY_PREFLIGHT_TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read().decode("utf-8"))

    if not isinstance(payload, dict):
        raise ValueError("model list response was not a JSON object")
    return payload
