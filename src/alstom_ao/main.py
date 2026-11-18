"""FastAPI application factory and lifespan wiring."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from alstom_ao import __version__
from alstom_ao.api.deps import build_container
from alstom_ao.api.middleware import RequestContextMiddleware
from alstom_ao.api.routers import agents, approvals, health, openai, orchestrate, policies
from alstom_ao.config import Settings, get_settings
from alstom_ao.errors import AppError
from alstom_ao.logging_conf import configure_logging
from alstom_ao.orchestrator.graph import build_graph, checkpointer_for
from alstom_ao.orchestrator.service import RunService

logger = logging.getLogger(__name__)


async def initialize_app(app: FastAPI) -> None:
    """Create tables, open the checkpointer and compile the graph.

    Exposed separately from the lifespan so integration tests can initialise the
    app without an ASGI lifespan manager.

    Agents are never seeded here: the registry is populated through
    ``POST /v1/agents``. See docs/handover.md.
    """

    container = app.state.container
    settings: Settings = container.settings

    if settings.db_auto_create:
        if settings.is_production:
            logger.warning(
                "AO_DB_AUTO_CREATE is enabled in production. Prefer 'alembic upgrade head' so schema "
                "changes are versioned and reversible."
            )
        await container.database.create_all()

    checkpointer_cm = checkpointer_for(settings)
    checkpointer = await checkpointer_cm.__aenter__()
    app.state.checkpointer_cm = checkpointer_cm
    app.state.graph = build_graph(container.services, checkpointer)
    container.run_service = RunService(container.services, app.state.graph)
    logger.info("Alstom AO ready (env=%s, policy_mode=%s).", settings.environment, settings.policy_mode)


async def shutdown_app(app: FastAPI) -> None:
    container = app.state.container
    try:
        await container.adapters.aclose()
    finally:
        try:
            await container.tracing.shutdown()
        finally:
            cm = getattr(app.state, "checkpointer_cm", None)
            if cm is not None:
                try:
                    await cm.__aexit__(None, None, None)
                except Exception:  # pragma: no cover - best effort
                    pass
            await container.database.dispose()


def create_app(
    settings: Settings | None = None,
    *,
    a2a_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging("DEBUG" if settings.debug else "INFO")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await initialize_app(app)
        try:
            yield
        finally:
            await shutdown_app(app)

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description=(
            "Multi-agent orchestration layer: A2A registry, dynamic routing via a LiteLLM gateway, "
            "OPA policy, dynamic human approvals, and Langfuse tracing."
        ),
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    app.state.container = build_container(settings, a2a_transport=a2a_transport)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-Id", "X-Trace-Id"],
    )
    app.add_middleware(RequestContextMiddleware)

    app.include_router(health.router)
    app.include_router(agents.router)
    app.include_router(orchestrate.router)
    app.include_router(approvals.router)
    app.include_router(policies.router)
    app.include_router(openai.router)

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        body = {
            "error": {
                "code": exc.code,
                "message": exc.message,
                "request_id": request_id,
            }
        }
        if settings.debug and exc.details is not None:
            body["error"]["details"] = exc.details
        headers = {}
        if exc.code in {"unauthorized"}:
            headers["WWW-Authenticate"] = "Bearer"
        return JSONResponse(body, status_code=exc.status_code, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            {
                "error": {
                    "code": "invalid_input",
                    "message": "The request payload is not valid.",
                    "request_id": getattr(request.state, "request_id", None),
                    "issues": exc.errors() if settings.debug else None,
                }
            },
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:  # pragma: no cover
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            {
                "error": {
                    "code": "internal_error",
                    "message": "Unexpected error.",
                    "request_id": getattr(request.state, "request_id", None),
                }
            },
            status_code=500,
        )

    @app.get("/", include_in_schema=False)
    async def root() -> dict:
        return {
            "service": settings.app_name,
            "version": __version__,
            "model": settings.virtual_model_id,
            "docs": "/docs",
            "health": "/healthz",
        }

    return app


app = create_app()
