"""Controlled vocabularies shared by the registry, orchestrator and API."""

from __future__ import annotations

from enum import StrEnum


class AgentProtocol(StrEnum):
    A2A = "a2a"
    MCP = "mcp"
    REST = "rest"


class AgentStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    DEPRECATED = "deprecated"


class AgentHealth(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def rank(self) -> int:
        return {RiskLevel.LOW: 1, RiskLevel.MEDIUM: 2, RiskLevel.HIGH: 3}[self]


class RunStatus(StrEnum):
    PENDING = "pending"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    COMPLETED = "completed"
    NO_MATCH = "no_match"
    DENIED = "denied"
    REJECTED = "rejected"
    INVALID = "invalid"
    FAILED = "failed"
    TIMEOUT = "timeout"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class TraceStatus(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    RETRIED = "retried"
    DENIED = "denied"
    PENDING = "pending"


class ActionType(StrEnum):
    """The action class an agent will perform on the user's behalf."""

    READ = "read"
    QUERY = "query"
    ANALYZE = "analyze"
    WRITE = "write"
    UPDATE = "update"
    DELETE = "delete"
    SEND = "send"
    EXECUTE = "execute"
    APPROVE = "approve"
    PAYMENT = "payment"

    @property
    def is_high_impact(self) -> bool:
        return self in {
            ActionType.WRITE,
            ActionType.UPDATE,
            ActionType.DELETE,
            ActionType.SEND,
            ActionType.EXECUTE,
            ActionType.PAYMENT,
        }


class TraceAction(StrEnum):
    RUN_STARTED = "run.started"
    INPUT_VALIDATED = "input.validated"
    BUDGET_WARNING = "budget.warning"
    BUDGET_DEGRADED = "budget.degraded"
    BUDGET_EXCEEDED = "budget.exceeded"
    HOP_LIMIT_HIT = "hop.limit_hit"
    PLAN_CREATED = "plan.created"
    NO_MATCH = "plan.no_match"
    POLICY_EVALUATED = "policy.evaluated"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_DECIDED = "approval.decided"
    AGENT_INVOKED = "agent.invoked"
    AGENT_COMPLETED = "agent.completed"
    AGENT_FAILED = "agent.failed"
    AGENT_RETRIED = "agent.retried"
    SYNTHESIS_COMPLETED = "synthesis.completed"
    SYNTHESIS_UNGROUNDED = "synthesis.ungrounded"
    GUARDRAIL_TRIGGERED = "guardrail.triggered"
    AGENT_SCHEMA_VIOLATION = "agent.schema_violation"
    AGENTS_INCONSISTENT = "agents.inconsistent"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
