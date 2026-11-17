"""Policy endpoints: dry-run evaluation and engine status."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from alstom_ao.api.deps import AppContainer, get_container, get_identity
from alstom_ao.api.schemas import PolicyEvaluateRequest
from alstom_ao.domain.models import IdentityContext, PolicyDecision

router = APIRouter(prefix="/v1/policies", tags=["policies"])


@router.get("/status")
async def policy_status(
    container: AppContainer = Depends(get_container),
    _: IdentityContext = Depends(get_identity),
) -> dict:
    return await container.policy.health()


@router.post("/evaluate", response_model=PolicyDecision)
async def evaluate_policy(
    payload: PolicyEvaluateRequest,
    identity: IdentityContext = Depends(get_identity),
    container: AppContainer = Depends(get_container),
) -> PolicyDecision:
    card = await container.registry.get(payload.agent_id)
    return await container.policy.evaluate_agent(
        identity=identity,
        agent=card,
        action=payload.action,
        environment=payload.environment or container.settings.environment,
        use_case=payload.use_case,
        query=payload.query,
    )
