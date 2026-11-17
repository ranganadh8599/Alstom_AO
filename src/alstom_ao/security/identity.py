"""Dual identity handling.

Two identities travel through every hop:

* **user identity** - who is asking (from the UI's trusted headers)
* **service identity** - which authenticated service is forwarding the request

Neither is ever inferred from the agent: the agent identity comes from the
registry card and is stamped onto the trace separately.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from alstom_ao.config import Settings
from alstom_ao.domain.models import IdentityContext, ServiceIdentity, UserIdentity
from alstom_ao.errors import UnauthorizedError

HEADER_USER_ID = "x-user-id"
HEADER_USER_EMAIL = "x-user-email"
HEADER_USER_NAME = "x-user-name"
HEADER_USER_ROLES = "x-user-roles"
HEADER_USER_TEAM = "x-user-team"
HEADER_TENANT_ID = "x-tenant-id"
HEADER_USE_CASE = "x-use-case"
HEADER_SERVICE_ID = "x-service-id"
HEADER_SERVICE_NAME = "x-service-name"
HEADER_SERVICE_SCOPES = "x-service-scopes"
HEADER_TRACE_ID = "x-trace-id"
HEADER_WORKFLOW_ID = "x-workflow-id"
HEADER_RUN_ID = "x-run-id"
HEADER_IDEMPOTENCY_KEY = "idempotency-key"

# Headers propagated to downstream A2A agents in addition to the metadata payload.
PROPAGATION_PREFIX = "x-alstom-"


def _split(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.replace(";", ",").split(",") if item.strip()]


def _fingerprint(token: str | None) -> str | None:
    if not token:
        return None
    digest = hashlib.sha256(token.encode()).hexdigest()[:16]
    return f"sha256:{digest}"


def _header(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


def extract_identity(headers: Mapping[str, str], settings: Settings, *, client_ip: str | None = None) -> IdentityContext:
    """Build both identities from trusted request headers.

    The UI is expected to authenticate the user and forward these headers. In a
    deployment where the gateway terminates auth, keep ``AO_TRUST_FORWARDED_IDENTITY``
    true and strip these headers from untrusted clients at the edge.
    """

    if not settings.trust_forwarded_identity:
        raise UnauthorizedError("Forwarded identity headers are not trusted in this deployment.")

    user_id = _header(headers, HEADER_USER_ID)
    if not user_id and settings.require_user_identity:
        raise UnauthorizedError(
            "Missing X-User-Id header. The UI must forward the authenticated user identity."
        )
    user_id = user_id or "anonymous"

    roles = _split(_header(headers, HEADER_USER_ROLES)) or settings.default_roles

    authorization = _header(headers, "authorization") or ""
    token = authorization[7:] if authorization.lower().startswith("bearer ") else None

    user = UserIdentity(
        user_id=user_id,
        email=_header(headers, HEADER_USER_EMAIL),
        name=_header(headers, HEADER_USER_NAME),
        roles=roles,
        team=_header(headers, HEADER_USER_TEAM),
        tenant_id=_header(headers, HEADER_TENANT_ID),
    )
    service = ServiceIdentity(
        service_id=_header(headers, HEADER_SERVICE_ID) or settings.service_id,
        service_name=_header(headers, HEADER_SERVICE_NAME) or settings.service_name,
        auth_method="bearer" if token else "forwarded_headers",
        token_fingerprint=_fingerprint(token),
        scopes=_split(_header(headers, HEADER_SERVICE_SCOPES)),
        client_ip=client_ip,
    )
    return IdentityContext(user=user, service=service)


def use_case_from_headers(headers: Mapping[str, str]) -> str:
    return (_header(headers, HEADER_USE_CASE) or "general").strip()[:128]


def propagation_headers(trace: dict, identity: IdentityContext) -> dict[str, str]:
    """Headers sent to downstream agents so they can join the same trace.

    Only non-sensitive identifiers are propagated. Tokens are never copied.
    """

    return {
        f"{PROPAGATION_PREFIX}trace-id": str(trace.get("trace_id", "")),
        f"{PROPAGATION_PREFIX}workflow-id": str(trace.get("workflow_id", "")),
        f"{PROPAGATION_PREFIX}run-id": str(trace.get("run_id", "")),
        f"{PROPAGATION_PREFIX}agent-id": str(trace.get("agent_id", "")),
        f"{PROPAGATION_PREFIX}agent-version": str(trace.get("agent_version", "")),
        f"{PROPAGATION_PREFIX}initiated-by": identity.user.user_id,
        f"{PROPAGATION_PREFIX}service": identity.service.service_id,
        f"{PROPAGATION_PREFIX}use-case": str(trace.get("use_case", "general")),
        f"{PROPAGATION_PREFIX}action": str(trace.get("action", "query")),
    }


def resolve_secret(secret_ref: str | None, settings: Settings) -> str | None:
    """Resolve ``env:NAME`` references. Raw secrets are never persisted in cards."""

    return settings.resolve_secret_ref(secret_ref)
