"""Health, readiness and metrics."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, Request
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

    # Agents are only reachable through the LiteLLM gateway: without it no agent
    # can be called, so readiness must reflect it rather than reporting "ok"
    # while every run would fail.
    gateway_status = "unreachable"
    gateway_ok = False
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(f"{container.settings.litellm_proxy_base}/health/liveliness")
            if response.status_code == 200:
                gateway_status = "ok"
                gateway_ok = True
            else:
                gateway_status = f"http_{response.status_code}"
    except httpx.HTTPError as exc:
        gateway_status = f"unreachable: {type(exc).__name__}"

    healthy = database_ok and gateway_ok
    payload = {
        "status": "ok" if healthy else "degraded",
        "checks": {
            "database": "ok" if database_ok else "error",
            "database_error": database_error,
            "adapters": container.adapters.protocols,
            "agent_gateway": gateway_status,
            "policy": await container.policy.health(),
            "langfuse": "enabled" if container.tracing.client is not None else "disabled",
        },
    }
    return payload if healthy else JSONResponse(payload, status_code=503)


@router.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics(
    request: Request,
    container: AppContainer = Depends(get_container),
) -> PlainTextResponse:
    if not container.settings.metrics_enabled:
        return PlainTextResponse("metrics disabled\n", status_code=404)
    # Metrics expose operational volume; require a token when one is configured
    # so the endpoint can be scraped by Prometheus without being world-readable.
    expected = container.settings.metrics_token
    if expected:
        provided = request.headers.get("x-metrics-token") or request.headers.get("authorization", "")
        provided = provided.removeprefix("Bearer ").strip()
        if provided != expected:
            return PlainTextResponse("forbidden\n", status_code=403)
    return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4")
