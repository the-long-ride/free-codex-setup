"""Dependency injection for FastAPI."""

import hmac

from fastapi import Depends, HTTPException, Request
from loguru import logger
from starlette.applications import Starlette

from config.settings import Settings
from config.settings import get_settings as _get_settings
from core.anthropic import get_user_facing_error_message
from providers.base import BaseProvider
from providers.exceptions import (
    AuthenticationError,
    ServiceUnavailableError,
    UnknownProviderTypeError,
)
from providers.registry import PROVIDER_DESCRIPTORS, ProviderRegistry

_DEFAULT_LOCALHOST_NETWORKS = frozenset({"127.0.0.1", "::1", "0.0.0.0", "::"})


def _request_is_local(request: Request) -> bool:
    """Return whether the request originates from the local machine."""

    client = request.client
    if client is not None:
        host = client.host
        if host in _DEFAULT_LOCALHOST_NETWORKS:
            return True
        if host == "::ffff:127.0.0.1":
            return True
    forwarded = (
        request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip") or ""
    )
    if forwarded:
        first_ip = forwarded.split(",")[0].strip()
        if first_ip in _DEFAULT_LOCALHOST_NETWORKS or first_ip == "::ffff:127.0.0.1":
            return True
    return False


# Process-level cache: only for :func:`get_provider_for_type` / :func:`get_provider`
# when there is no ``Request``/``app`` (unit tests, scripts). HTTP handlers must pass
# ``app`` to :func:`resolve_provider` so the app-scoped registry is used.
_providers: dict[str, BaseProvider] = {}


def get_settings() -> Settings:
    """Return cached :class:`~config.settings.Settings` (FastAPI-friendly alias)."""
    return _get_settings()


def resolve_provider(
    provider_type: str,
    *,
    app: Starlette | None,
    settings: Settings,
) -> BaseProvider:
    """Resolve a provider using the app-scoped registry when ``app`` is set.

    When ``app`` is not ``None``, the app-owned :attr:`app.state.provider_registry`
    must exist (installed by :class:`~api.runtime.AppRuntime` during startup).
    Callers that construct a bare ``FastAPI`` without lifespan must set
    ``app.state.provider_registry`` explicitly.

    When ``app`` is ``None`` (no HTTP context), uses the process-level
    :data:`_providers` cache only.
    """
    if app is not None:
        reg = getattr(app.state, "provider_registry", None)
        if reg is None:
            raise ServiceUnavailableError(
                "Provider registry is not configured. Ensure AppRuntime startup ran "
                "or assign app.state.provider_registry for test apps."
            )
        return _resolve_with_registry(reg, provider_type, settings)
    return _resolve_with_registry(ProviderRegistry(_providers), provider_type, settings)


def _resolve_with_registry(
    registry: ProviderRegistry, provider_type: str, settings: Settings
) -> BaseProvider:
    should_log_init = not registry.is_cached(provider_type)
    try:
        provider = registry.get(provider_type, settings)
    except AuthenticationError as e:
        # Provider :class:`~providers.exceptions.AuthenticationError` messages are
        # curated configuration hints (env var names, docs links), not upstream noise.
        detail = str(e).strip() or get_user_facing_error_message(e)
        raise HTTPException(status_code=503, detail=detail) from e
    except UnknownProviderTypeError:
        logger.error(
            "Unknown provider_type: '{}'. Supported: {}",
            provider_type,
            ", ".join(f"'{key}'" for key in PROVIDER_DESCRIPTORS),
        )
        raise
    if should_log_init:
        logger.info("Provider initialized: {}", provider_type)
    return provider


def get_provider_for_type(provider_type: str) -> BaseProvider:
    """Get or create a provider in the process-level cache (no ``app``/Request).

    HTTP route handlers should call :func:`resolve_provider` with the active
    :attr:`request.app` (via :class:`~api.runtime.AppRuntime`) instead of this
    process-wide cache.
    """
    return resolve_provider(provider_type, app=None, settings=get_settings())


def require_api_key(
    request: Request, settings: Settings = Depends(get_settings)
) -> str | None:
    """Require the configured server auth token for client API requests.

    Checks `x-api-key`, `Authorization: Bearer ...`, or
    `anthropic-auth-token` against `Settings.anthropic_auth_token`. If
    `ANTHROPIC_AUTH_TOKEN` is empty, client auth is disabled.
    """

    configured_token = settings.anthropic_auth_token.strip()
    if not configured_token:
        return None

    header = (
        request.headers.get("x-api-key")
        or request.headers.get("authorization")
        or request.headers.get("anthropic-auth-token")
    )
    if not header:
        raise HTTPException(status_code=401, detail="Missing API key")

    token = header.strip()
    if header.lower().startswith("bearer "):
        token = header.split(" ", 1)[1].strip()

    if token and ":" in token:
        token = token.split(":", 1)[0].strip()

    if configured_token == "freecc" and not _request_is_local(request):
        raise HTTPException(status_code=401, detail="Invalid API key")
    if not hmac.compare_digest(token.encode("utf-8"), configured_token.encode("utf-8")):
        raise HTTPException(status_code=401, detail="Invalid API key")
    return None


def get_provider() -> BaseProvider:
    """Get or create the default provider (``MODEL`` / ``provider_type``).

    Process-cache helper for scripts, unit tests, and non-FastAPI callers. HTTP
    handlers must use :func:`resolve_provider` with :attr:`request.app` so the
    app-scoped :class:`~providers.registry.ProviderRegistry` is used.
    """
    return get_provider_for_type(get_settings().provider_type)


async def cleanup_provider():
    """Cleanup all provider resources."""
    global _providers
    await ProviderRegistry(_providers).cleanup()
    _providers = {}
    logger.debug("Provider cleanup completed")
