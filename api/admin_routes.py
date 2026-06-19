"""Local admin UI routes and APIs."""

from __future__ import annotations

import inspect
import ipaddress
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from cli.codex_model_catalog import build_codex_model_catalog
from config.settings import Settings
from config.settings import get_settings as get_cached_settings
from core.codex_config import (
    CODEX_MODEL_CATALOG_DOWNLOAD_FILENAME,
    render_codex_config_snippet,
)
from providers.registry import ProviderRegistry

from .access_control import (
    ADMIN_SESSION_COOKIE,
    ADMIN_SESSION_MAX_AGE_SECONDS,
    admin_session_status,
    change_admin_password,
    create_admin_session,
    get_visible_model_ids,
    logout_admin_session,
    set_visible_model_ids,
)
from .admin_config import (
    FIELD_BY_KEY,
    load_config_response,
    provider_config_status,
    validate_updates,
    write_managed_env,
)
from .admin_urls import local_admin_url, local_proxy_root_url
from .model_catalog import build_models_list_response

router = APIRouter()

STATIC_DIR = Path(__file__).resolve().parent / "admin_static"
LOCAL_PROVIDER_PATHS = {
    "lmstudio": "/models",
    "llamacpp": "/models",
    "ollama": "/api/tags",
}


class AdminConfigPayload(BaseModel):
    """Partial config update submitted by the admin UI."""

    values: dict[str, Any] = Field(default_factory=dict)


class AdminLoginPayload(BaseModel):
    password: str = Field(default="")


class AdminPasswordChangePayload(BaseModel):
    current_password: str = Field(default="")
    new_password: str = Field(default="")


class ModelVisibilityPayload(BaseModel):
    model_ids: list[str] = Field(default_factory=list)


def _is_loopback_host(host: str | None) -> bool:
    if host is None:
        return False
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _origin_is_local(origin: str | None) -> bool:
    if not origin:
        return True
    parsed = urlsplit(origin)
    return _is_loopback_host(parsed.hostname)


def require_loopback_admin(request: Request) -> None:
    """Allow admin access only from the local machine."""

    client_host = request.client.host if request.client else None
    if not _is_loopback_host(client_host):
        raise HTTPException(status_code=403, detail="Admin UI is local-only")

    origin = request.headers.get("origin")
    if not _origin_is_local(origin):
        raise HTTPException(status_code=403, detail="Admin UI is local-only")


def require_admin_session(request: Request) -> None:
    """Require an active admin cookie session."""

    require_loopback_admin(request)
    session = admin_session_status(request.cookies.get(ADMIN_SESSION_COOKIE))
    if not session["authenticated"]:
        raise HTTPException(status_code=401, detail="Admin login required")


def _asset_response(filename: str) -> FileResponse:
    path = STATIC_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Admin asset not found")
    response = FileResponse(path)
    response.headers["Cache-Control"] = "no-store"
    return response


def _html_response(filename: str) -> FileResponse:
    response = _asset_response(filename)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/admin", include_in_schema=False)
async def admin_page(request: Request):
    require_loopback_admin(request)
    session = admin_session_status(request.cookies.get(ADMIN_SESSION_COOKIE))
    if not session["authenticated"]:
        return RedirectResponse("/admin/login", status_code=303)
    return _html_response("index.html")


@router.get("/admin/login", include_in_schema=False)
async def admin_login_page(request: Request):
    require_loopback_admin(request)
    session = admin_session_status(request.cookies.get(ADMIN_SESSION_COOKIE))
    if session["authenticated"]:
        return RedirectResponse("/admin", status_code=303)
    return _html_response("login.html")


@router.get("/admin/assets/{filename}", include_in_schema=False)
async def admin_asset(filename: str, request: Request):
    require_loopback_admin(request)
    if filename not in {"admin.css", "admin.js", "login.js"}:
        raise HTTPException(status_code=404, detail="Admin asset not found")
    return _asset_response(filename)


@router.get("/admin/api/config")
async def get_admin_config(request: Request):
    require_admin_session(request)
    return load_config_response()


@router.post("/admin/api/config/validate")
async def validate_admin_config(payload: AdminConfigPayload, request: Request):
    require_admin_session(request)
    return validate_updates(_filtered_values(payload.values))


@router.post("/admin/api/config/apply")
async def apply_admin_config(
    payload: AdminConfigPayload,
    request: Request,
    background_tasks: BackgroundTasks,
):
    require_admin_session(request)
    result = write_managed_env(_filtered_values(payload.values))
    if not result["applied"]:
        return result

    get_cached_settings.cache_clear()
    restart = _restart_metadata(result["pending_fields"], request)
    result["restart"] = restart
    if restart["required"] and restart["automatic"]:
        callback = request.app.state.admin_restart_callback
        background_tasks.add_task(_invoke_admin_restart_callback, callback)
        request.app.state.admin_pending_fields = []
        return result

    old_registry = getattr(request.app.state, "provider_registry", None)
    if isinstance(old_registry, ProviderRegistry):
        await old_registry.cleanup()
    request.app.state.provider_registry = ProviderRegistry()
    request.app.state.admin_pending_fields = result["pending_fields"]
    return result


@router.get("/admin/api/status")
async def admin_status(request: Request):
    require_admin_session(request)
    settings = get_cached_settings()
    registry = getattr(request.app.state, "provider_registry", None)
    cached_models: dict[str, list[str]] = {}
    if isinstance(registry, ProviderRegistry):
        cached_models = {
            provider_id: sorted(model_ids)
            for provider_id, model_ids in registry.cached_model_ids().items()
        }
    return {
        "status": "running",
        "host": settings.host,
        "port": settings.port,
        "model": settings.model,
        "provider": settings.provider_type,
        "pending_fields": getattr(request.app.state, "admin_pending_fields", []),
        "provider_status": provider_config_status(),
        "cached_models": cached_models,
    }


@router.get("/admin/api/providers/local-status")
async def local_provider_status(request: Request):
    require_admin_session(request)
    config = load_config_response()
    values = {field["key"]: field["value"] for field in config["fields"]}
    checks = []
    for provider_id, path in LOCAL_PROVIDER_PATHS.items():
        base_url = _local_provider_url(provider_id, values)
        checks.append(await _check_local_provider(provider_id, base_url, path))
    return {"providers": checks}


@router.post("/admin/api/providers/{provider_id}/test")
async def test_provider(provider_id: str, request: Request):
    require_admin_session(request)
    settings = get_cached_settings()
    registry = getattr(request.app.state, "provider_registry", None)
    if not isinstance(registry, ProviderRegistry):
        registry = ProviderRegistry()
        request.app.state.provider_registry = registry
    try:
        provider = registry.get(provider_id, settings)
        infos = await provider.list_model_infos()
    except Exception as exc:
        return {
            "provider_id": provider_id,
            "ok": False,
            "error_type": type(exc).__name__,
        }
    registry.cache_model_infos(provider_id, infos)
    return {
        "provider_id": provider_id,
        "ok": True,
        "models": sorted(info.model_id for info in infos),
    }


@router.post("/admin/api/models/refresh")
async def refresh_models(request: Request):
    require_admin_session(request)
    settings = get_cached_settings()
    registry = getattr(request.app.state, "provider_registry", None)
    if not isinstance(registry, ProviderRegistry):
        registry = ProviderRegistry()
        request.app.state.provider_registry = registry
    await registry.refresh_model_list_cache(settings)
    return {
        "cached_models": {
            provider_id: sorted(model_ids)
            for provider_id, model_ids in registry.cached_model_ids().items()
        }
    }


@router.get("/admin/api/session")
async def get_admin_session(request: Request):
    require_loopback_admin(request)
    return admin_session_status(request.cookies.get(ADMIN_SESSION_COOKIE))


@router.post("/admin/api/login")
async def login_admin(payload: AdminLoginPayload, request: Request):
    require_loopback_admin(request)
    session = create_admin_session(payload.password)
    if session is None:
        raise HTTPException(status_code=401, detail="Invalid password")
    response = JSONResponse(
        {
            "authenticated": True,
            "expires_at": session["expires_at"],
        }
    )
    response.set_cookie(
        ADMIN_SESSION_COOKIE,
        session["session_id"],
        httponly=True,
        samesite="strict",
        max_age=ADMIN_SESSION_MAX_AGE_SECONDS,
        secure=False,
    )
    return response


@router.post("/admin/api/logout")
async def logout_admin(request: Request):
    require_loopback_admin(request)
    logout_admin_session(request.cookies.get(ADMIN_SESSION_COOKIE))
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(ADMIN_SESSION_COOKIE)
    return response


@router.get("/admin/api/access")
async def get_access_state(request: Request):
    require_admin_session(request)
    settings = get_cached_settings()
    registry = getattr(request.app.state, "provider_registry", None)
    provider_registry = registry if isinstance(registry, ProviderRegistry) else None
    full_models = build_models_list_response(settings, provider_registry)
    explicit_visible_ids = get_visible_model_ids()
    visible_ids = set(explicit_visible_ids or [])
    visibility_configured = explicit_visible_ids is not None
    return {
        "password_change_supported": True,
        "model_visibility_configured": visibility_configured,
        "visible_model_ids": sorted(visible_ids),
        "available_models": [
            {
                "id": model.id,
                "display_name": model.display_name,
                "enabled": model.id in visible_ids if visibility_configured else True,
            }
            for model in full_models.data
        ],
    }


@router.post("/admin/api/access/password")
async def update_access_password(payload: AdminPasswordChangePayload, request: Request):
    require_admin_session(request)
    if len(payload.new_password.strip()) == 0:
        raise HTTPException(status_code=400, detail="New password cannot be blank")
    if not change_admin_password(payload.current_password, payload.new_password):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    response = JSONResponse({"updated": True})
    response.delete_cookie(ADMIN_SESSION_COOKIE)
    return response


@router.get("/admin/api/access/codex-config")
async def get_access_codex_config(request: Request):
    require_admin_session(request)
    settings = get_cached_settings()
    snippet = render_codex_config_snippet(
        base_url=f"{local_proxy_root_url(settings).rstrip('/')}/v1",
        api_key=settings.anthropic_auth_token,
    )
    return {
        "auth_source": "env_key",
        "snippet": snippet,
        "catalog_filename": CODEX_MODEL_CATALOG_DOWNLOAD_FILENAME,
    }


@router.get("/admin/api/access/model-catalog")
async def download_access_model_catalog(request: Request):
    require_admin_session(request)
    settings = get_cached_settings()
    registry = getattr(request.app.state, "provider_registry", None)
    provider_registry = registry if isinstance(registry, ProviderRegistry) else None
    visible_model_ids = get_visible_model_ids()
    models_response = build_models_list_response(
        settings,
        provider_registry,
        visible_model_ids=(
            set(visible_model_ids) if visible_model_ids is not None else None
        ),
    )
    catalog = build_codex_model_catalog(models_response.model_dump(mode="json"))
    return JSONResponse(
        catalog,
        headers={
            "Content-Disposition": (
                f'attachment; filename="{CODEX_MODEL_CATALOG_DOWNLOAD_FILENAME}"'
            )
        },
    )


@router.post("/admin/api/access/model-visibility")
async def update_model_visibility(payload: ModelVisibilityPayload, request: Request):
    require_admin_session(request)
    return {"visible_model_ids": set_visible_model_ids(payload.model_ids)}


def _filtered_values(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if key in FIELD_BY_KEY}


async def _invoke_admin_restart_callback(callback: Any) -> None:
    result = callback()
    if inspect.isawaitable(result):
        await result


def _restart_metadata(fields: list[str], request: Request) -> dict[str, Any]:
    callback = getattr(request.app.state, "admin_restart_callback", None)
    automatic = bool(fields and callable(callback))
    return {
        "required": bool(fields),
        "automatic": automatic,
        "admin_url": _next_admin_url() if automatic else None,
        "fields": fields,
    }


def _next_admin_url() -> str:
    fields = {
        field["key"]: field["value"] for field in load_config_response()["fields"]
    }
    settings = Settings.model_construct(
        host=fields.get("HOST") or "0.0.0.0",
        port=int(fields.get("PORT") or 8082),
    )
    return local_admin_url(settings)


def _local_provider_url(provider_id: str, values: dict[str, str]) -> str:
    if provider_id == "lmstudio":
        return values.get("LM_STUDIO_BASE_URL", "")
    if provider_id == "llamacpp":
        return values.get("LLAMACPP_BASE_URL", "")
    if provider_id == "ollama":
        return values.get("OLLAMA_BASE_URL", "")
    return ""


async def _check_local_provider(
    provider_id: str, base_url: str, path: str
) -> dict[str, Any]:
    clean_url = base_url.strip().rstrip("/")
    if not clean_url:
        return {
            "provider_id": provider_id,
            "status": "missing_url",
            "label": "Missing URL",
            "base_url": base_url,
        }

    url = f"{clean_url}{path}"
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            response = await client.get(url)
        ok = 200 <= response.status_code < 300
        return {
            "provider_id": provider_id,
            "status": "reachable" if ok else "offline",
            "label": "Reachable" if ok else "Offline",
            "base_url": base_url,
            "status_code": response.status_code,
        }
    except Exception as exc:
        return {
            "provider_id": provider_id,
            "status": "offline",
            "label": "Offline",
            "base_url": base_url,
            "error_type": type(exc).__name__,
        }
