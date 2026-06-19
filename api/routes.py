"""FastAPI route handlers."""

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from loguru import logger

from config.provider_ids import SUPPORTED_PROVIDER_IDS
from config.settings import Settings
from core.anthropic import get_token_count
from core.trace import trace_event
from providers.registry import ProviderRegistry

from . import dependencies
from .access_control import get_visible_model_ids
from .dependencies import get_settings, require_api_key
from .gateway_model_ids import (
    decode_gateway_model_id,
    gateway_model_id,
    no_thinking_gateway_model_id,
)
from .model_catalog import build_models_list_response
from .models.anthropic import MessagesRequest, TokenCountRequest
from .models.openai_responses import OpenAIResponsesRequest
from .models.responses import ModelsListResponse
from .request_pipeline import ApiRequestPipeline

router = APIRouter()


def _provider_registry_from_request(request: Request) -> ProviderRegistry | None:
    registry = getattr(request.app.state, "provider_registry", None)
    return registry if isinstance(registry, ProviderRegistry) else None


def _enabled_model_ids(request: Request) -> set[str] | None:
    visible_model_ids = get_visible_model_ids()
    if visible_model_ids is None:
        return None
    return set(visible_model_ids)


def _client_access_model_ids(model_id: str) -> set[str]:
    """Return ids equivalent to one client-requested model for access checks."""

    ids = {model_id}
    decoded = decode_gateway_model_id(model_id)
    if decoded is not None:
        provider_model_ref = f"{decoded.provider_id}/{decoded.provider_model}"
        ids.add(provider_model_ref)
        ids.add(gateway_model_id(provider_model_ref))
        ids.add(no_thinking_gateway_model_id(provider_model_ref))
        return ids

    provider_id, separator, provider_model = model_id.partition("/")
    if separator and provider_model and provider_id in SUPPORTED_PROVIDER_IDS:
        ids.add(gateway_model_id(model_id))
        ids.add(no_thinking_gateway_model_id(model_id))
    return ids


def _require_enabled_model(model_id: str, request: Request) -> None:
    enabled_model_ids = _enabled_model_ids(request)
    if enabled_model_ids is None:
        return
    if enabled_model_ids.isdisjoint(_client_access_model_ids(model_id)):
        raise HTTPException(
            status_code=403, detail="Model is not enabled for client access"
        )


def get_request_pipeline(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> ApiRequestPipeline:
    """Build the API request pipeline for route handlers."""
    return ApiRequestPipeline(
        settings,
        provider_getter=lambda provider_type: dependencies.resolve_provider(
            provider_type, app=request.app, settings=settings
        ),
        token_counter=get_token_count,
    )


def _probe_response(allow: str) -> Response:
    """Return an empty success response for compatibility probes."""
    return Response(status_code=204, headers={"Allow": allow})


# =============================================================================
# Routes
# =============================================================================
@router.post("/v1/messages")
async def create_message(
    request_data: MessagesRequest,
    request: Request,
    pipeline: ApiRequestPipeline = Depends(get_request_pipeline),
    _auth=Depends(require_api_key),
):
    """Create a message (always streaming)."""
    _require_enabled_model(request_data.model, request)
    return pipeline.create_message(request_data)


@router.api_route("/v1/messages", methods=["HEAD", "OPTIONS"])
async def probe_messages(_auth=Depends(require_api_key)):
    """Respond to Claude compatibility probes for the messages endpoint."""
    return _probe_response("POST, HEAD, OPTIONS")


@router.post("/v1/responses")
async def create_response(
    request_data: OpenAIResponsesRequest,
    request: Request,
    pipeline: ApiRequestPipeline = Depends(get_request_pipeline),
    _auth=Depends(require_api_key),
):
    """Create an OpenAI Responses-compatible response through this proxy."""
    _require_enabled_model(request_data.model, request)
    return await pipeline.create_response(request_data)


@router.api_route("/v1/responses", methods=["HEAD", "OPTIONS"])
async def probe_responses(_auth=Depends(require_api_key)):
    """Respond to OpenAI Responses compatibility probes."""
    return _probe_response("POST, HEAD, OPTIONS")


@router.post("/v1/messages/count_tokens")
async def count_tokens(
    request_data: TokenCountRequest,
    request: Request,
    pipeline: ApiRequestPipeline = Depends(get_request_pipeline),
    _auth=Depends(require_api_key),
):
    """Count tokens for a request."""
    _require_enabled_model(request_data.model, request)
    return pipeline.count_tokens(request_data)


@router.api_route("/v1/messages/count_tokens", methods=["HEAD", "OPTIONS"])
async def probe_count_tokens(_auth=Depends(require_api_key)):
    """Respond to Claude compatibility probes for the token count endpoint."""
    return _probe_response("POST, HEAD, OPTIONS")


@router.get("/")
async def root(
    settings: Settings = Depends(get_settings), _auth=Depends(require_api_key)
):
    """Root endpoint."""
    return {
        "status": "ok",
        "provider": settings.provider_type,
        "model": settings.model,
    }


@router.api_route("/", methods=["HEAD", "OPTIONS"])
async def probe_root():
    """Respond to unauthenticated local compatibility probes for the root endpoint."""
    return _probe_response("GET, HEAD, OPTIONS")


@router.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "healthy"}


@router.api_route("/health", methods=["HEAD", "OPTIONS"])
async def probe_health():
    """Respond to compatibility probes for the health endpoint."""
    return _probe_response("GET, HEAD, OPTIONS")


@router.get("/v1/models", response_model=ModelsListResponse)
async def list_models(
    request: Request,
    settings: Settings = Depends(get_settings),
    _auth=Depends(require_api_key),
):
    """List the model ids this proxy advertises to Claude-compatible clients."""
    trace_event(stage="ingress", event="api.models.list", source="api")
    return build_models_list_response(
        settings,
        _provider_registry_from_request(request),
        visible_model_ids=(
            set(visible_model_ids)
            if (visible_model_ids := get_visible_model_ids()) is not None
            else None
        ),
    )


@router.post("/stop")
async def stop_cli(request: Request, _auth=Depends(require_api_key)):
    """Stop all CLI sessions and pending tasks."""
    handler = getattr(request.app.state, "message_handler", None)
    if not handler:
        # Fallback if messaging not initialized
        cli_manager = getattr(request.app.state, "cli_manager", None)
        if cli_manager:
            await cli_manager.stop_all()
            logger.info("STOP_CLI: source=cli_manager cancelled_count=N/A")
            return {"status": "stopped", "source": "cli_manager"}
        raise HTTPException(status_code=503, detail="Messaging system not initialized")

    count = await handler.stop_all_tasks()
    trace_event(
        stage="ingress",
        event="api.cli.stop_via_handler",
        source="api",
        cancelled_nodes=count,
    )
    logger.info("STOP_CLI: source=handler cancelled_count={}", count)
    return {"status": "stopped", "cancelled_count": count}
