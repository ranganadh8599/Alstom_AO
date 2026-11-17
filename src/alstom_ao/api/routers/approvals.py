"""Human approval queue: list, inspect, decide.

Approving is a two-step operation: the decision is recorded (with an OPA policy
check on *who* may approve), then the paused graph is resumed with the decision.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from alstom_ao.api.deps import AppContainer, get_container, get_identity, get_run_service
from alstom_ao.api.schemas import ApprovalDecisionRequest, ApprovalDecisionResponse
from alstom_ao.domain.models import ApprovalRequest, IdentityContext
from alstom_ao.orchestrator.service import RunService

router = APIRouter(prefix="/v1/approvals", tags=["approvals"])


@router.get("")
async def list_approvals(
    status_filter: str = Query(default="pending", alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    container: AppContainer = Depends(get_container),
    _: IdentityContext = Depends(get_identity),
) -> dict:
    if status_filter != "pending":
        return {"items": [], "total": 0, "note": "only 'pending' is supported"}
    items = await container.approvals.list_pending(limit=limit)
    return {"items": [item.model_dump(mode="json") for item in items], "total": len(items)}


@router.get("/{approval_id}", response_model=ApprovalRequest)
async def get_approval(
    approval_id: str,
    container: AppContainer = Depends(get_container),
    _: IdentityContext = Depends(get_identity),
) -> ApprovalRequest:
    return await container.approvals.get(approval_id)


@router.post("/{approval_id}/decision", response_model=ApprovalDecisionResponse)
async def decide_approval(
    approval_id: str,
    payload: ApprovalDecisionRequest,
    identity: IdentityContext = Depends(get_identity),
    container: AppContainer = Depends(get_container),
    run_service: RunService = Depends(get_run_service),
) -> ApprovalDecisionResponse:
    approval = await container.approvals.decide(
        approval_id,
        approved=payload.approved,
        actor=identity,
        note=payload.note,
    )
    run = await run_service.resume(
        run_id=approval.run_id,
        approved=payload.approved,
        actor=identity,
        note=payload.note,
    )
    refreshed = await container.approvals.get(approval_id)
    return ApprovalDecisionResponse(approval=refreshed, run=run)
