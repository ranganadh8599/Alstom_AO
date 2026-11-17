"""API request/response schemas (domain cards live in ``alstom_ao.domain.models``)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from alstom_ao.domain.enums import ActionType, RiskLevel
from alstom_ao.domain.models import AgentCard, ApprovalRequest, PolicyDecision, RunResult
from alstom_ao.errors import InputValidationError


class ConversationTurn(BaseModel):
    role: Literal["user", "assistant"] = "user"
    content: str = Field(max_length=4000)


class OrchestrateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=6000)
    environment: str | None = None
    use_case: str | None = Field(default=None, max_length=128)
    conversation: list[ConversationTurn] = Field(default_factory=list, max_length=40)

    @field_validator("query", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


def validate_idempotency_key(value: str | None) -> str | None:
    """Bound the header before it reaches the database.

    A 128-char column with a unique constraint means an oversized or malformed key
    would surface as a 500 on Postgres. Reject it as a client error instead.
    """

    if value is None:
        return None
    key = value.strip()
    if not key:
        return None
    if len(key) > 128:
        raise InputValidationError("Idempotency-Key must be 128 characters or fewer.")
    if not key.isprintable():
        raise InputValidationError("Idempotency-Key must contain printable characters only.")
    return key


class OrchestrateResponse(BaseModel):
    run: RunResult


class AgentListResponse(BaseModel):
    items: list[AgentCard]
    total: int


class AgentCreateResponse(BaseModel):
    agent: AgentCard
    created: bool = True


class DiscoverRequest(BaseModel):
    endpoint: str
    environment: str | None = None
    owner: str | None = None
    risk_level: RiskLevel = RiskLevel.LOW
    tags: list[str] = Field(default_factory=list)


class HealthResponse(BaseModel):
    agent_id: str
    health: str


class ApprovalDecisionRequest(BaseModel):
    approved: bool
    note: str | None = Field(default=None, max_length=2000)


class ApprovalDecisionResponse(BaseModel):
    approval: ApprovalRequest
    run: RunResult


class PolicyEvaluateRequest(BaseModel):
    agent_id: str
    action: ActionType = ActionType.QUERY
    environment: str | None = None
    use_case: str = "policy_dry_run"
    query: str = "dry run"


class TraceEventResponse(BaseModel):
    events: list[dict[str, Any]]


class PolicyDecisionResponse(BaseModel):
    decisions: list[PolicyDecision]


# --- OpenAI-compatible surface ------------------------------------------------


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"] = "user"
    content: str | None = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str = "alstom-ao"
    messages: list[ChatMessage]
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    user: str | None = None
    metadata: dict[str, Any] | None = None

    @property
    def last_user_message(self) -> str:
        for message in reversed(self.messages):
            if message.role == "user" and message.content:
                return message.content
        return ""

    @property
    def conversation(self) -> list[dict[str, str]]:
        return [
            {"role": message.role, "content": message.content or ""}
            for message in self.messages
            if message.role in {"user", "assistant"} and message.content
        ][:-1]


class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: dict[str, Any]
    finish_reason: str | None = "stop"


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[ChatCompletionChoice]
    usage: dict[str, int]
    alstom_ao: dict[str, Any] = Field(default_factory=dict)


class ModelInfo(BaseModel):
    id: str
    object: str = "model"
    owned_by: str = "alstom-ao"
    created: int = 0


class ModelListResponse(BaseModel):
    object: str = "list"
    data: list[ModelInfo]
