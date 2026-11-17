"""Composition root and FastAPI dependencies."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import Depends, Request

from alstom_ao.adapters.factory import AdapterRegistry
from alstom_ao.approvals.service import ApprovalService
from alstom_ao.config import Settings
from alstom_ao.db.session import Database
from alstom_ao.domain.models import IdentityContext
from alstom_ao.errors import ForbiddenError
from alstom_ao.observability.tracing import TracingService
from alstom_ao.orchestrator.planner import Planner
from alstom_ao.orchestrator.runtime import NodeServices
from alstom_ao.orchestrator.service import RunService
from alstom_ao.policy.service import PolicyService
from alstom_ao.registry.service import AgentRegistryService
from alstom_ao.security.identity import extract_identity, use_case_from_headers

REGISTRY_ADMIN_ROLES = {"platform-admin", "registry-admin"}


@dataclass
class AppContainer:
    settings: Settings
    database: Database
    adapters: AdapterRegistry
    registry: AgentRegistryService
    policy: PolicyService
    approvals: ApprovalService
    tracing: TracingService
    planner: Planner
    services: NodeServices
    run_service: RunService | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def build_container(settings: Settings, *, a2a_transport: httpx.AsyncBaseTransport | None = None) -> AppContainer:
    database = Database(settings)
    adapters = AdapterRegistry.default(settings, transport=a2a_transport)
    registry = AgentRegistryService(database, settings)
    policy = PolicyService(settings)
    approvals = ApprovalService(database, policy, settings)
    tracing = TracingService(settings, database)
    planner = Planner(settings)
    services = NodeServices(
        settings=settings,
        database=database,
        registry=registry,
        policy=policy,
        approvals=approvals,
        adapters=adapters,
        planner=planner,
        tracing=tracing,
    )
    return AppContainer(
        settings=settings,
        database=database,
        adapters=adapters,
        registry=registry,
        policy=policy,
        approvals=approvals,
        tracing=tracing,
        planner=planner,
        services=services,
    )


def get_container(request: Request) -> AppContainer:
    return request.app.state.container


def get_run_service(container: AppContainer = Depends(get_container)) -> RunService:
    if container.run_service is None:  # pragma: no cover - lifespan guarantees this
        raise RuntimeError("Run service is not initialised; the application lifespan did not run.")
    return container.run_service


async def get_identity(
    request: Request,
    container: AppContainer = Depends(get_container),
) -> IdentityContext:
    client_ip = request.client.host if request.client else None
    identity = extract_identity(request.headers, container.settings, client_ip=client_ip)
    request.state.identity = identity
    request.state.use_case = use_case_from_headers(request.headers)
    return identity


def resolve_identity(
    request: Request,
    settings: Settings,
    *,
    fallback_user_id: str | None = None,
) -> IdentityContext:
    """Identity for endpoints that must read the body before authenticating.

    The OpenAI-compatible surface accepts ``user`` as a *fallback* identifier when
    the UI did not forward ``X-User-Id``; headers always win.
    """

    headers = {key.lower(): value for key, value in request.headers.items()}
    if fallback_user_id and "x-user-id" not in headers:
        headers["x-user-id"] = fallback_user_id
    client_ip = request.client.host if request.client else None
    return extract_identity(headers, settings, client_ip=client_ip)


async def require_registry_admin(identity: IdentityContext = Depends(get_identity)) -> IdentityContext:
    if not identity.user.has_any_role(sorted(REGISTRY_ADMIN_ROLES)):
        raise ForbiddenError(
            "Registering or changing agents requires the platform-admin or registry-admin role.",
            details={"required_roles": sorted(REGISTRY_ADMIN_ROLES)},
        )
    return identity
