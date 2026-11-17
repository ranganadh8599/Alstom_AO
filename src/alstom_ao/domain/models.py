"""Domain models: agent identity cards, user/service identity and trace context.

These are the contracts of the platform. An agent cannot be registered without a
complete capability card, and every invocation carries the full trace context.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from alstom_ao.domain.enums import (
    ActionType,
    AgentHealth,
    AgentProtocol,
    AgentStatus,
    ApprovalStatus,
    RiskLevel,
    RunStatus,
    TraceStatus,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


class AgentAuth(BaseModel):
    """How the orchestrator authenticates *to* the agent.

    Secrets are referenced, never stored: use ``env:VARIABLE_NAME``.
    """

    type: Literal["none", "bearer", "api_key", "oauth2"] = "none"
    secret_ref: str | None = None
    header_name: str = "Authorization"
    scopes: list[str] = Field(default_factory=list)
    token_url: str | None = None


class AgentPermissions(BaseModel):
    """What the agent may do on the user's behalf."""

    allowed_roles: list[str] = Field(default_factory=list)
    allowed_actions: list[ActionType] = Field(default_factory=list)
    high_impact_actions: list[ActionType] = Field(default_factory=list)
    required_scopes: list[str] = Field(default_factory=list)
    data_classification: str = "internal"
    requires_approval_for: list[ActionType] = Field(default_factory=list)

    def allows_action(self, action: ActionType) -> bool:
        # An empty allow-list means "read-only by default", never "everything".
        if not self.allowed_actions:
            return not action.is_high_impact
        return action in self.allowed_actions

    @property
    def approval_actions(self) -> set[ActionType]:
        return set(self.requires_approval_for) | set(self.high_impact_actions)


class CapabilityDetail(BaseModel):
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)


class AgentCard(BaseModel):
    """The mandatory identity + capability card of every integrated agent."""

    agent_id: str = Field(min_length=2, max_length=128, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
    name: str = Field(min_length=2, max_length=200)
    description: str = Field(min_length=10, max_length=4000)
    platform: str = Field(default="custom", max_length=64)
    protocol: AgentProtocol = AgentProtocol.A2A
    endpoint: str = Field(min_length=8, max_length=500)
    owner: str = Field(default="", max_length=200)
    owner_contact: str | None = None
    capabilities: list[str] = Field(min_length=1)
    capability_details: list[CapabilityDetail] = Field(default_factory=list)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    auth: AgentAuth = Field(default_factory=AgentAuth)
    permissions: AgentPermissions = Field(default_factory=AgentPermissions)
    version: str = Field(default="1.0.0", max_length=32)
    status: AgentStatus = AgentStatus.ACTIVE
    environment: str = Field(default="dev", max_length=24)
    risk_level: RiskLevel = RiskLevel.LOW
    tags: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    health: AgentHealth = AgentHealth.UNKNOWN
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @field_validator("endpoint")
    @classmethod
    def _endpoint_must_be_url(cls, value: str) -> str:
        if not (value.startswith("http://") or value.startswith("https://")):
            raise ValueError("endpoint must start with http:// or https://")
        return value.rstrip("/")

    @field_validator("capabilities", "tags", "examples")
    @classmethod
    def _clean_list(cls, value: list[str]) -> list[str]:
        seen: list[str] = []
        for item in value:
            item = item.strip()
            if item and item.lower() not in {v.lower() for v in seen}:
                seen.append(item)
        return seen

    @model_validator(mode="after")
    def _infer_approval_actions(self) -> AgentCard:
        if self.risk_level == RiskLevel.HIGH and not self.permissions.requires_approval_for:
            # High-risk agents always route consequential actions through human
            # approval unless the card narrows it explicitly. Read-only actions
            # stay fast: safety must not turn every question into a ticket.
            self.permissions.requires_approval_for = [
                action for action in self.permissions.allowed_actions if action.is_high_impact
            ]
        return self

    @property
    def is_callable(self) -> bool:
        return self.status == AgentStatus.ACTIVE

    @property
    def capability_text(self) -> str:
        parts = [self.name, self.description, *self.capabilities, *self.tags, *self.examples]
        return " ".join(parts).lower()


class _CardEndpointValidation(BaseModel):
    """Shared endpoint validation so *all* card payloads are rejected early."""

    @field_validator("endpoint", check_fields=False)
    @classmethod
    def _endpoint_must_be_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not (value.startswith("http://") or value.startswith("https://")):
            raise ValueError("endpoint must start with http:// or https://")
        return value.rstrip("/")


class AgentCardCreate(_CardEndpointValidation):
    """Card payload accepted by the registry (timestamps are server-owned)."""

    model_config = ConfigDict(extra="forbid")

    agent_id: str
    name: str
    description: str
    platform: str = "custom"
    protocol: AgentProtocol = AgentProtocol.A2A
    endpoint: str
    owner: str = ""
    owner_contact: str | None = None
    capabilities: list[str]
    capability_details: list[CapabilityDetail] = Field(default_factory=list)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    auth: AgentAuth = Field(default_factory=AgentAuth)
    permissions: AgentPermissions = Field(default_factory=AgentPermissions)
    version: str = "1.0.0"
    status: AgentStatus = AgentStatus.ACTIVE
    environment: str = "dev"
    risk_level: RiskLevel = RiskLevel.LOW
    tags: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentCardUpdate(_CardEndpointValidation):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    platform: str | None = None
    protocol: AgentProtocol | None = None
    endpoint: str | None = None
    owner: str | None = None
    owner_contact: str | None = None
    capabilities: list[str] | None = None
    capability_details: list[CapabilityDetail] | None = None
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    auth: AgentAuth | None = None
    permissions: AgentPermissions | None = None
    version: str | None = None
    status: AgentStatus | None = None
    environment: str | None = None
    risk_level: RiskLevel | None = None
    tags: list[str] | None = None
    examples: list[str] | None = None
    metadata: dict[str, Any] | None = None


class UserIdentity(BaseModel):
    """Identity of the human on whose behalf the workflow runs."""

    user_id: str
    email: str | None = None
    name: str | None = None
    roles: list[str] = Field(default_factory=lambda: ["user"])
    team: str | None = None
    tenant_id: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)

    def has_any_role(self, roles: list[str]) -> bool:
        wanted = {role.lower() for role in roles}
        return bool({role.lower() for role in self.roles} & wanted)


class ServiceIdentity(BaseModel):
    """Identity of the calling service (the UI backend / gateway)."""

    service_id: str = "alstom-ao"
    service_name: str = "agent-orchestrator"
    auth_method: str = "forwarded_headers"
    token_fingerprint: str | None = None
    scopes: list[str] = Field(default_factory=list)
    client_ip: str | None = None


class IdentityContext(BaseModel):
    """Both identities, preserved end to end through every hop."""

    user: UserIdentity
    service: ServiceIdentity

    def as_metadata(self) -> dict[str, Any]:
        return {
            "user": self.user.model_dump(mode="json"),
            "service": self.service.model_dump(mode="json"),
        }


class TraceContext(BaseModel):
    """The common envelope attached to every agent invocation and trace event."""

    workflow_id: str
    trace_id: str
    run_id: str
    span_id: str | None = None
    parent_span_id: str | None = None
    agent_id: str
    agent_version: str
    agent_name: str | None = None
    protocol: str
    action: ActionType = ActionType.QUERY
    initiated_by: str
    authenticated_service: str
    use_case: str = "general"
    environment: str = "dev"
    timestamp: datetime = Field(default_factory=utcnow)
    status: TraceStatus = TraceStatus.STARTED
    retry_count: int = 0
    previous_agents: list[str] = Field(default_factory=list)
    next_agents: list[str] = Field(default_factory=list)

    def as_metadata(self) -> dict[str, Any]:
        """Wire format sent to downstream A2A agents (never includes secrets)."""
        return self.model_dump(mode="json")


class InvocationRequest(BaseModel):
    """One call from the orchestration layer to one agent."""

    query: str
    trace: TraceContext
    identity: IdentityContext
    instructions: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)


class InvocationResult(BaseModel):
    agent_id: str
    agent_name: str
    agent_version: str
    protocol: str
    status: TraceStatus
    text: str = ""
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    structured: dict[str, Any] = Field(default_factory=dict)
    latency_ms: int = 0
    retry_count: int = 0
    error: str | None = None
    raw: dict[str, Any] | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == TraceStatus.SUCCEEDED


class PlannedAgent(BaseModel):
    agent_id: str
    reason: str = ""
    instructions: str = ""
    capability: str | None = None
    action: ActionType = ActionType.QUERY
    priority: int = 1


class Plan(BaseModel):
    intent: str = ""
    use_case: str = "general"
    summary: str = ""
    selected_agents: list[PlannedAgent] = Field(default_factory=list)
    requires_human_approval: bool = False
    approval_reason: str | None = None
    confidence: float = 0.0
    no_match_reason: str | None = None
    source: str = "llm"  # llm | heuristic | hybrid
    rejected_agents: list[dict[str, Any]] = Field(default_factory=list)


class ApprovalAssessment(BaseModel):
    required: bool = False
    risk_level: RiskLevel = RiskLevel.LOW
    reasons: list[str] = Field(default_factory=list)
    actions: list[ActionType] = Field(default_factory=list)


class ApprovalRequest(BaseModel):
    approval_id: str
    run_id: str
    agent_ids: list[str] = Field(default_factory=list)
    action: ActionType = ActionType.QUERY
    risk_level: RiskLevel = RiskLevel.LOW
    reasons: list[str] = Field(default_factory=list)
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime | None = None
    decided_at: datetime | None = None
    decided_by: str | None = None
    decision_note: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class PolicyDecision(BaseModel):
    allowed: bool
    reasons: list[str] = Field(default_factory=list)
    obligations: dict[str, Any] = Field(default_factory=dict)
    source: str = "local"
    raw: dict[str, Any] = Field(default_factory=dict)
    agent_id: str = ""
    action: ActionType = ActionType.QUERY
    requires_approval: bool = False


class TraceEvent(BaseModel):
    run_id: str
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    sequence: int = 0
    agent_id: str | None = None
    agent_version: str | None = None
    protocol: str | None = None
    action: str
    status: TraceStatus = TraceStatus.STARTED
    initiated_by: str | None = None
    authenticated_service: str | None = None
    use_case: str | None = None
    timestamp: datetime = Field(default_factory=utcnow)
    input: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    latency_ms: int | None = None
    retry_count: int = 0
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunRecord(BaseModel):
    run_id: str
    workflow_id: str
    trace_id: str
    status: RunStatus
    query: str
    environment: str
    use_case: str
    identity: IdentityContext
    plan: Plan | None = None
    selected_agents: list[str] = Field(default_factory=list)
    response: str = ""
    error: str | None = None
    approval_id: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    langfuse_url: str | None = None
    idempotency_key: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    completed_at: datetime | None = None


class RunResult(BaseModel):
    """Stable response shape for both the native and the OpenAI-compatible API."""

    run_id: str
    workflow_id: str
    trace_id: str
    status: RunStatus
    response: str
    plan: Plan | None = None
    agent_results: list[InvocationResult] = Field(default_factory=list)
    approval: ApprovalRequest | None = None
    policy_decisions: list[PolicyDecision] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    langfuse_url: str | None = None
    duration_ms: int = 0
