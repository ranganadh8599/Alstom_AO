"""OPA client and local policy evaluation.

Policy is evaluated **before** any agent is called, for every selected agent.
Denied agents never run. High-risk actions produce an approval obligation.

Modes
-----
``observe``   evaluate policy, record the decision, but do not block the run.
``enforce``   a denied decision blocks that agent (and fails closed if OPA is
              unreachable and no local fallback is configured).
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from alstom_ao.config import Settings
from alstom_ao.domain.enums import ActionType, AgentStatus, RiskLevel
from alstom_ao.domain.models import AgentCard, IdentityContext, PolicyDecision

logger = logging.getLogger(__name__)

HIGH_IMPACT_ACTIONS = {action.value for action in ActionType if action.is_high_impact}
APPROVER_ROLES = {"admin"}


def build_policy_input(
    *,
    identity: IdentityContext,
    agent: AgentCard,
    action: ActionType,
    environment: str,
    use_case: str,
    query: str,
) -> dict[str, Any]:
    return {
        "user": {
            "user_id": identity.user.user_id,
            "roles": identity.user.roles,
            "team": identity.user.team,
            "tenant_id": identity.user.tenant_id,
        },
        "service": {
            "service_id": identity.service.service_id,
            "scopes": identity.service.scopes,
        },
        "agent": {
            "agent_id": agent.agent_id,
            "name": agent.name,
            "version": agent.version,
            "status": str(agent.status),
            "environment": agent.environment,
            "risk_level": str(agent.risk_level),
            "owner": agent.owner,
            "permissions": agent.permissions.model_dump(mode="json"),
            "metadata": agent.metadata,
        },
        "action": str(action),
        "environment": environment,
        "request": {"use_case": use_case, "query": query[:2000]},
    }


class OPAClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        # Optional bearer for OPA deployments that require authentication.
        token = os.environ.get("AO_OPA_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def decide(self, policy_path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Return OPA's result document, or ``None`` when OPA is unavailable."""

        if not self.settings.opa_url:
            return None
        url = f"{self.settings.opa_url.rstrip('/')}/v1/data/{policy_path.strip('/')}"
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.opa_timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.post(url, json={"input": payload}, headers=self._headers())
                response.raise_for_status()
                body = response.json()
                result = body.get("result")
                return result if isinstance(result, dict) else {}
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("OPA decision unavailable at %s: %s", url, exc)
            return None


def local_decision(
    *,
    identity: IdentityContext,
    agent: AgentCard,
    action: ActionType,
    environment: str,
) -> PolicyDecision:
    """Deterministic fallback that mirrors ``policies/agent_authz.rego``."""

    reasons: list[str] = []
    roles = {role.lower() for role in identity.user.roles}
    is_admin = "admin" in roles

    if agent.status != AgentStatus.ACTIVE:
        reasons.append("agent_not_active")
    if agent.environment != environment and not is_admin:
        reasons.append("environment_mismatch")
    if agent.permissions.allowed_roles:
        allowed = {role.lower() for role in agent.permissions.allowed_roles}
        if not (roles & allowed) and not is_admin:
            reasons.append("role_not_permitted")
    if not agent.permissions.allows_action(action) and not is_admin:
        reasons.append("action_not_permitted")
    if agent.permissions.required_scopes:
        if not set(agent.permissions.required_scopes).issubset(set(identity.service.scopes)) and not is_admin:
            reasons.append("service_scope_missing")

    tenant = agent.metadata.get("tenant_id") if isinstance(agent.metadata, dict) else None
    if tenant and identity.user.tenant_id and tenant != identity.user.tenant_id and not is_admin:
        reasons.append("tenant_mismatch")

    if agent.permissions.data_classification == "restricted":
        if not (roles & {"admin"}):
            reasons.append("restricted_data_role_missing")

    approved_action = action in agent.permissions.approval_actions
    high_risk_action = agent.risk_level == RiskLevel.HIGH and action.is_high_impact
    requires_approval = approved_action or high_risk_action

    return PolicyDecision(
        allowed=not reasons,
        reasons=reasons,
        obligations={"requires_approval": requires_approval},
        source="local",
        agent_id=agent.agent_id,
        action=action,
        requires_approval=requires_approval,
    )


class PolicyService:
    def __init__(self, settings: Settings, opa: OPAClient | None = None) -> None:
        self.settings = settings
        self.opa = opa or OPAClient(settings)

    async def health(self) -> dict[str, Any]:
        """Report whether OPA is reachable; never raises."""

        if not self.settings.opa_url:
            return {"opa": "not_configured", "mode": self.settings.policy_mode, "local_fallback": self.settings.policy_local_fallback}
        try:
            async with httpx.AsyncClient(timeout=self.settings.opa_timeout_seconds) as client:
                response = await client.get(f"{self.settings.opa_url.rstrip('/')}/health")
                status = "healthy" if response.status_code == 200 else f"http_{response.status_code}"
        except httpx.HTTPError:
            status = "unreachable"
        return {
            "opa": status,
            "mode": self.settings.policy_mode,
            "local_fallback": self.settings.policy_local_fallback,
            "decision_path": self.settings.opa_decision_path,
        }

    async def evaluate_agent(
        self,
        *,
        identity: IdentityContext,
        agent: AgentCard,
        action: ActionType,
        environment: str,
        use_case: str,
        query: str,
    ) -> PolicyDecision:
        fallback = local_decision(identity=identity, agent=agent, action=action, environment=environment)
        payload = build_policy_input(
            identity=identity,
            agent=agent,
            action=action,
            environment=environment,
            use_case=use_case,
            query=query,
        )
        result = await self.opa.decide(self.settings.opa_decision_path, payload)

        if result is None:
            if not self.settings.policy_local_fallback:
                decision = PolicyDecision(
                    allowed=self.settings.policy_mode == "observe",
                    reasons=["policy_engine_unavailable"]
                    if self.settings.policy_mode != "observe"
                    else ["policy_engine_unavailable_observed"],
                    source="opa-unavailable",
                    agent_id=agent.agent_id,
                    action=action,
                    requires_approval=fallback.requires_approval,
                )
            else:
                decision = fallback.model_copy(
                    update={
                        "source": "local-fallback",
                        "reasons": [*fallback.reasons, "opa_unavailable"],
                    }
                )
        else:
            decision = PolicyDecision(
                allowed=bool(result.get("allow", False)),
                reasons=[str(reason) for reason in result.get("reasons", [])],
                obligations=result.get("obligations") or {},
                source="opa",
                raw=result,
                agent_id=agent.agent_id,
                action=action,
                requires_approval=bool(
                    (result.get("obligations") or {}).get("requires_approval", fallback.requires_approval)
                ),
            )

        if not decision.allowed and self.settings.policy_mode == "observe":
            # Observe mode records the denial but lets the run continue. Reasons
            # stay attached so the trace shows exactly what would have blocked it.
            decision.allowed = True
            decision.reasons = [*decision.reasons, "denied_in_observe_mode"]
        return decision

    async def evaluate_approval(
        self,
        *,
        identity: IdentityContext,
        requested_by: str,
        risk_level: RiskLevel,
        action: ActionType,
    ) -> PolicyDecision:
        """Who may approve: an approver role, and never the requester themselves."""

        roles = {role.lower() for role in identity.user.roles}
        reasons: list[str] = []
        if not (roles & APPROVER_ROLES):
            reasons.append("approver_role_required")
        if identity.user.user_id == requested_by:
            reasons.append("cannot_approve_own_request")

        payload = {
            "user": identity.user.model_dump(mode="json"),
            "requested_by": requested_by,
            "risk_level": str(risk_level),
            "action": str(action),
        }
        result = await self.opa.decide("alstom/approval/decision", payload)
        if isinstance(result, dict) and "allow" in result:
            return PolicyDecision(
                allowed=bool(result.get("allow")),
                reasons=[str(reason) for reason in result.get("reasons", [])] or reasons,
                source="opa",
                raw=result,
                action=action,
            )
        return PolicyDecision(allowed=not reasons, reasons=reasons, source="local", action=action)
