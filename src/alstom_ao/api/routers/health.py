"""Health, readiness and metrics."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, PlainTextResponse

from alstom_ao import __version__
from alstom_ao.api.deps import AppContainer, get_container
from alstom_ao.observability.metrics import metrics

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz(container: AppContainer = Depends(get_container)) -> dict:
    return {
        "status": "ok",
        "service": container.settings.app_name,
        "version": __version__,
        "environment": container.settings.environment,
    }


@router.get("/readyz")
async def readyz(container: AppContainer = Depends(get_container)):
    database_ok = True
    database_error = None
    try:
        await container.database.ping()
    except Exception as exc:
        database_ok = False
        database_error = str(exc)

    payload = {
        "status": "ok" if database_ok else "degraded",
        "checks": {
            "database": "ok" if database_ok else "error",
            "database_error": database_error,
            "adapters": container.adapters.protocols,
            "policy": await container.policy.health(),
            "langfuse": "enabled" if container.tracing.client is not None else "disabled",
            "llm_mode": container.settings.llm_mode,
        },
    }
    return payload if database_ok else JSONResponse(payload, status_code=503)


@router.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics(container: AppContainer = Depends(get_container)) -> PlainTextResponse:
    if not container.settings.metrics_enabled:
        return PlainTextResponse("metrics disabled\n", status_code=404)
    return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4")
