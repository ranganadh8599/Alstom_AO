"""Typed application errors with stable machine-readable codes."""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for all expected failures.

    ``code`` is part of the API contract and is safe to show to clients;
    ``details`` is never included in production responses.
    """

    status_code: int = 500
    code: str = "internal_error"
    default_message: str = "Unexpected error"

    def __init__(
        self,
        message: str | None = None,
        *,
        details: Any = None,
        code: str | None = None,
        status_code: int | None = None,
    ) -> None:
        self.message = message or self.default_message
        self.details = details
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code
        super().__init__(self.message)


class InputValidationError(AppError):
    status_code = 422
    code = "invalid_input"
    default_message = "The request is not valid."


class UnauthorizedError(AppError):
    status_code = 401
    code = "unauthorized"
    default_message = "Missing or invalid caller identity."


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"
    default_message = "You are not allowed to perform this action."


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"
    default_message = "Resource not found."


class ConflictError(AppError):
    status_code = 409
    code = "conflict"
    default_message = "The resource already exists or is in a conflicting state."


class UnsupportedProtocolError(AppError):
    status_code = 501
    code = "unsupported_protocol"
    default_message = "No adapter is registered for this agent protocol."


class PolicyDeniedError(AppError):
    status_code = 403
    code = "policy_denied"
    default_message = "Policy denied this agent invocation."


class ApprovalRequiredError(AppError):
    status_code = 409
    code = "approval_required"
    default_message = "This action requires human approval."


class ApprovalExpiredError(AppError):
    status_code = 409
    code = "approval_expired"
    default_message = "The approval request has expired."


class BudgetExceededError(AppError):
    status_code = 429
    code = "budget_exceeded"
    default_message = "The run exceeded its LLM budget."


class AgentInvocationError(AppError):
    status_code = 502
    code = "agent_invocation_failed"
    default_message = "The downstream agent could not complete the request."


class AgentTimeoutError(AgentInvocationError):
    status_code = 504
    code = "agent_timeout"
    default_message = "The downstream agent timed out."


class UpstreamUnavailableError(AppError):
    status_code = 503
    code = "upstream_unavailable"
    default_message = "A required upstream dependency is unavailable."
