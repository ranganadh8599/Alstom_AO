"""Dynamic human-in-the-loop risk assessment and approval persistence.

The decision to ask a human is **not** a static flag on an agent. It combines:

* the intent-level judgement of the planner (``requires_human_approval``)
* the concrete action each agent will take (read vs write/delete/pay)
* the agent's declared risk level and ``requires_approval_for`` actions
* policy obligations returned by OPA
* the caller's roles (approver bypass is explicit and off by default)

Deterministic guardrails win over the model: destructive actions always pause,
even if the planner forgot to ask.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from alstom_ao.config import Settings
from alstom_ao.db.repositories import ApprovalRepository, is_expired
from alstom_ao.db.session import Database
from alstom_ao.domain.enums import ActionType, ApprovalStatus, RiskLevel
from alstom_ao.domain.models import (
    AgentCard,
    ApprovalAssessment,
    ApprovalRequest,
    IdentityContext,
    Plan,
    PolicyDecision,
)
from alstom_ao.errors import ApprovalExpiredError, ForbiddenError, NotFoundError
from alstom_ao.policy.service import PolicyService

ALWAYS_APPROVE_ACTIONS = {ActionType.DELETE, ActionType.PAYMENT, ActionType.EXECUTE}
RISK_BY_ACTION = {
    ActionType.DELETE: RiskLevel.HIGH,
    ActionType.PAYMENT: RiskLevel.HIGH,
    ActionType.EXECUTE: RiskLevel.HIGH,
    ActionType.WRITE: RiskLevel.MEDIUM,
    ActionType.UPDATE: RiskLevel.MEDIUM,
    ActionType.SEND: RiskLevel.MEDIUM,
}


def assess_approval(
    *,
    plan: Plan,
    cards: dict[str, AgentCard],
    decisions: list[PolicyDecision],
    identity: IdentityContext,
    settings: Settings,
) -> ApprovalAssessment:
    reasons: list[str] = []
    actions: list[ActionType] = []
    risk = RiskLevel.LOW

    for planned in plan.selected_agents:
        card = cards.get(planned.agent_id)
        action = planned.action
        actions.append(action)

        action_risk = RISK_BY_ACTION.get(action, RiskLevel.LOW)
        if action_risk.rank > risk.rank:
            risk = action_risk
        if card and card.risk_level.rank > risk.rank:
            risk = card.risk_level

        if action in ALWAYS_APPROVE_ACTIONS:
            reasons.append(f"action '{action}' is destructive or financially binding")
        if card and action in card.permissions.approval_actions:
            reasons.append(f"{card.name} requires approval for '{action}'")
        if card and card.risk_level == RiskLevel.HIGH and action.is_high_impact:
            reasons.append(f"{card.name} is classified high risk")

    for decision in decisions:
        if decision.requires_approval and decision.allowed:
            reasons.append(f"policy obligation for '{decision.agent_id}' requires approval")

    if plan.requires_human_approval:
        reasons.append(plan.approval_reason or "the planner judged the request consequential")

    threshold = _risk_from_string(settings.approval_risk_threshold)
    threshold_hit = risk.rank >= threshold.rank and any(action.is_high_impact for action in actions)

    required = bool(reasons) or threshold_hit
    if required and identity.user.has_any_role(settings.approval_admin_role_list) and settings.approval_admin_bypass:
        required = False
        reasons.append("admin_bypass_applied")

    deduped = list(dict.fromkeys(reasons))
    return ApprovalAssessment(required=required, risk_level=risk, reasons=deduped, actions=actions)


def _risk_from_string(value: str) -> RiskLevel:
    try:
        return RiskLevel(value.lower())
    except ValueError:
        return RiskLevel.HIGH


class ApprovalService:
    def __init__(self, database: Database, policy: PolicyService, settings: Settings) -> None:
        self.database = database
        self.policy = policy
        self.settings = settings

    async def get_or_create(
        self,
        *,
        run_id: str,
        assessment: ApprovalAssessment,
        plan: Plan,
        query: str,
        identity: IdentityContext,
    ) -> tuple[ApprovalRequest, bool]:
        async with self.database.session() as session:
            repository = ApprovalRepository(session)
            existing = await repository.get_by_run(run_id)
            if existing:
                return existing, False

            now = datetime.now(UTC)
            request = ApprovalRequest(
                approval_id=f"apv_{uuid.uuid4().hex[:20]}",
                run_id=run_id,
                agent_ids=[agent.agent_id for agent in plan.selected_agents],
                action=assessment.actions[0] if assessment.actions else ActionType.QUERY,
                risk_level=assessment.risk_level,
                reasons=assessment.reasons,
                status=ApprovalStatus.PENDING,
                requested_at=now,
                expires_at=now + timedelta(minutes=self.settings.approval_ttl_minutes),
                payload={
                    "query": query[:2000],
                    "requested_by": identity.user.user_id,
                    "requested_by_email": identity.user.email,
                    "tenant_id": identity.user.tenant_id,
                    "use_case": plan.use_case,
                    "agents": [
                        {
                            "agent_id": agent.agent_id,
                            "action": str(agent.action),
                            "reason": agent.reason,
                        }
                        for agent in plan.selected_agents
                    ],
                },
            )
            return await repository.create(request), True

    async def get(self, approval_id: str) -> ApprovalRequest:
        async with self.database.session() as session:
            request = await ApprovalRepository(session).get(approval_id)
        if not request:
            raise NotFoundError(f"Approval '{approval_id}' does not exist.")
        return request

    async def get_by_run(self, run_id: str) -> ApprovalRequest | None:
        async with self.database.session() as session:
            return await ApprovalRepository(session).get_by_run(run_id)

    async def list_pending(
        self,
        *,
        identity: IdentityContext | None = None,
        limit: int = 100,
    ) -> list[ApprovalRequest]:
        async with self.database.session() as session:
            repository = ApprovalRepository(session)
            pending = await repository.list_pending(limit=limit)
            for request in pending:
                if is_expired(request.expires_at):
                    await repository.expire(request.approval_id)
        active = [request for request in pending if not is_expired(request.expires_at)]
        if identity is None:
            return active
        # Only queue items the caller may actually act on.
        from alstom_ao.security.access import can_view_approval

        return [
            request
            for request in active
            if can_view_approval(request, identity, self.settings)
        ]

    async def decide(
        self,
        approval_id: str,
        *,
        approved: bool,
        actor: IdentityContext,
        note: str | None = None,
    ) -> ApprovalRequest:
        request = await self.get(approval_id)
        if request.status != ApprovalStatus.PENDING:
            return request
        if is_expired(request.expires_at):
            async with self.database.session() as session:
                await ApprovalRepository(session).expire(approval_id)
            raise ApprovalExpiredError(
                f"Approval '{approval_id}' expired at "
                f"{request.expires_at.isoformat() if request.expires_at else 'unknown'}."
            )

        requested_by = (request.payload or {}).get("requested_by", "")
        decision = await self.policy.evaluate_approval(
            identity=actor,
            requested_by=requested_by,
            risk_level=request.risk_level,
            action=request.action,
        )
        if not decision.allowed and not self.settings.approval_allow_self:
            raise ForbiddenError(
                "You are not allowed to decide this approval.",
                details={"reasons": decision.reasons},
            )

        async with self.database.session() as session:
            return await ApprovalRepository(session).decide(
                approval_id,
                approved=approved,
                decided_by=actor.user.user_id,
                note=note,
            )

    async def record_decision_in_graph(
        self,
        approval_id: str,
        *,
        approved: bool,
        decided_by: str,
        note: str | None,
    ) -> ApprovalRequest:
        """Idempotent write used by the graph when it resumes with a decision."""

        async with self.database.session() as session:
            repository = ApprovalRepository(session)
            current = await repository.get(approval_id)
            if not current:
                raise NotFoundError(f"Approval '{approval_id}' does not exist.")
            if current.status != ApprovalStatus.PENDING:
                return current
            return await repository.decide(
                approval_id,
                approved=approved,
                decided_by=decided_by,
                note=note,
            )
