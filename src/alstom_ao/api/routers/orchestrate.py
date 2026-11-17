"""Native orchestration API and run inspection."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from alstom_ao.api.deps import AppContainer, get_container, get_identity, get_run_service
from alstom_ao.api.schemas import (
    OrchestrateRequest,
    OrchestrateResponse,
    PolicyDecisionResponse,
    TraceEventResponse,
    validate_idempotency_key,
)
from alstom_ao.domain.models import IdentityContext, RunRecord
from alstom_ao.orchestrator.service import RunService
from alstom_ao.security.access import require_run_access
from alstom_ao.security.identity import HEADER_IDEMPOTENCY_KEY, hop_depth_from_headers

router = APIRouter(prefix="/v1", tags=["orchestration"])


@router.post("/orchestrate", response_model=OrchestrateResponse)
async def orchestrate(
    payload: OrchestrateRequest,
    request: Request,
    identity: IdentityContext = Depends(get_identity),
    run_service: RunService = Depends(get_run_service),
) -> OrchestrateResponse:
    result = await run_service.start(
        query=payload.query,
        identity=identity,
        environment=payload.environment,
        use_case=payload.use_case or getattr(request.state, "use_case", "general"),
        conversation=[turn.model_dump() for turn in payload.conversation],
        trace_id=request.headers.get("x-trace-id"),
        workflow_id=request.headers.get("x-workflow-id"),
        idempotency_key=validate_idempotency_key(request.headers.get(HEADER_IDEMPOTENCY_KEY)),
        hop_depth=hop_depth_from_headers(request.headers),
    )
    return OrchestrateResponse(run=result)


@router.get("/runs")
async def list_runs(
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    identity: IdentityContext = Depends(get_identity),
    run_service: RunService = Depends(get_run_service),
) -> dict:
    runs = await run_service.list_runs(
        user_id=identity.user.user_id,
        tenant_id=identity.user.tenant_id,
        status=status_filter,
        limit=limit,
        offset=offset,
    )
    return {"items": [run.model_dump(mode="json") for run in runs], "total": len(runs)}


@router.get("/runs/{run_id}", response_model=RunRecord)
async def get_run(
    run_id: str,
    identity: IdentityContext = Depends(get_identity),
    run_service: RunService = Depends(get_run_service),
    container: AppContainer = Depends(get_container),
) -> RunRecord:
    record = await run_service.get_run(run_id)
    require_run_access(record, identity, container.settings)
    return record


@router.get("/runs/{run_id}/trace", response_model=TraceEventResponse)
async def get_run_trace(
    run_id: str,
    limit: int = Query(default=500, ge=1, le=2000),
    identity: IdentityContext = Depends(get_identity),
    run_service: RunService = Depends(get_run_service),
    container: AppContainer = Depends(get_container),
) -> TraceEventResponse:
    record = await run_service.get_run(run_id)
    require_run_access(record, identity, container.settings)
    events = await run_service.get_trace(run_id)
    return TraceEventResponse(events=[event.model_dump(mode="json") for event in events[:limit]])


@router.get("/runs/{run_id}/policy", response_model=PolicyDecisionResponse)
async def get_run_policy(
    run_id: str,
    identity: IdentityContext = Depends(get_identity),
    run_service: RunService = Depends(get_run_service),
    container: AppContainer = Depends(get_container),
) -> PolicyDecisionResponse:
    record = await run_service.get_run(run_id)
    require_run_access(record, identity, container.settings)
    decisions = await run_service.get_policy_decisions(run_id)
    return PolicyDecisionResponse(decisions=decisions)


@router.get("/runs/{run_id}/timeline")
async def get_run_timeline(
    run_id: str,
    identity: IdentityContext = Depends(get_identity),
    run_service: RunService = Depends(get_run_service),
    container: AppContainer = Depends(get_container),
) -> dict:
    """The 'which agents ran before/after' view, reconstructed from the ledger."""

    record = await run_service.get_run(run_id)
    require_run_access(record, identity, container.settings)
    events = await run_service.get_trace(run_id)
    agents = [
        {
            "agent_id": event.agent_id,
            "agent_version": event.agent_version,
            "action": event.action,
            "status": str(event.status),
            "at": event.timestamp.isoformat(),
            "latency_ms": event.latency_ms,
            "retry_count": event.retry_count,
            "error": event.error,
        }
        for event in events
        if event.agent_id
    ]
    return {
        "run_id": run_id,
        "trace_id": record.trace_id,
        "status": str(record.status),
        "started_at": record.created_at.isoformat(),
        "completed_at": record.completed_at.isoformat() if record.completed_at else None,
        "agents": agents,
        "event_count": len(events),
    }
