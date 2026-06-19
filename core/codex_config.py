"""Shared Codex configuration helpers."""

from __future__ import annotations

import json
from pathlib import Path

FREE_CLAUDE_CODE_PROVIDER_NAME = "Free Claude Code"
DEFAULT_CODEX_MODEL_CATALOG_REFERENCE = "./codex-model-catalog.json"
CODEX_MODEL_CATALOG_DOWNLOAD_FILENAME = "codex-model-catalog.json"


def default_codex_model_catalog_path() -> str:
    """Return the default absolute Codex model catalog path."""

    return str(
        (Path.home() / ".codex" / CODEX_MODEL_CATALOG_DOWNLOAD_FILENAME).resolve()
    )


def fcc_provider_toml_entries(*, base_url: str, api_key: str) -> dict[str, str]:
    """Return TOML-ready values for the FCC Codex provider table."""

    return {
        "name": json.dumps(FREE_CLAUDE_CODE_PROVIDER_NAME),
        "base_url": json.dumps(base_url.rstrip("/")),
        "api_key": json.dumps(api_key.strip() or "fcc-no-auth"),
        "wire_api": json.dumps("responses"),
    }


def render_codex_config_snippet(
    *,
    base_url: str,
    api_key: str = "",
    catalog_path: str | None = None,
    model: str | None = None,
) -> str:
    """Render a minimal Codex config snippet for the FCC provider."""

    resolved_catalog_path = (
        default_codex_model_catalog_path() if catalog_path is None else catalog_path
    )
    resolved_api_key = api_key.strip() or "fcc-no-auth"
    lines = [f"model_provider = {json.dumps('fcc')}"]
    if resolved_catalog_path is not None:
        lines.append(f"model_catalog_json = {json.dumps(resolved_catalog_path)}")
    if model:
        lines.append(f"model = {json.dumps(model)}")
    lines.extend(
        [
            "",
            "[model_providers.fcc]",
            f"name = {json.dumps(FREE_CLAUDE_CODE_PROVIDER_NAME)}",
            f"base_url = {json.dumps(base_url.rstrip('/'))}",
            f"api_key = {json.dumps(resolved_api_key)}",
            f"wire_api = {json.dumps('responses')}",
            "",
        ]
    )
    return "\n".join(lines)
