"""Agent registry API.

Registration is admin-only; reading the catalog only needs an authenticated
caller. These endpoints are what makes the registry the *central* source of truth
rather than a config file.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response, status

from alstom_ao.api.deps import AppContainer, get_container, get_identity, require_registry_admin
from alstom_ao.api.schemas import (
    AgentCreateResponse,
    AgentListResponse,
    DiscoverRequest,
    HealthResponse,
)
from alstom_ao.domain.enums import AgentStatus
from alstom_ao.domain.models import AgentCard, AgentCardCreate, AgentCardUpdate, IdentityContext

router = APIRouter(prefix="/v1/agents", tags=["agents"])


@router.post("", response_model=AgentCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_agent(
    payload: AgentCardCreate,
    container: AppContainer = Depends(get_container),
    _: IdentityContext = Depends(require_registry_admin),
) -> AgentCreateResponse:
    card = await container.registry.register(payload)
    return AgentCreateResponse(agent=card, created=True)


@router.get("", response_model=AgentListResponse)
async def list_agents(
    environment: str | None = Query(default=None),
    status_filter: AgentStatus | None = Query(default=None, alias="status"),
    protocol: str | None = Query(default=None),
    capability: str | None = Query(default=None),
    q: str | None = Query(default=None, description="Free-text match on name/description"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    container: AppContainer = Depends(get_container),
    _: IdentityContext = Depends(get_identity),
) -> AgentListResponse:
    items = await container.registry.list(
        environment=environment,
        status=status_filter,
        protocol=protocol,
        capability=capability,
        query=q,
        limit=limit,
        offset=offset,
    )
    return AgentListResponse(items=items, total=await container.registry.count())


@router.get("/catalog")
async def get_catalog(
    environment: str | None = Query(default=None),
    container: AppContainer = Depends(get_container),
    identity: IdentityContext = Depends(get_identity),
) -> dict:
    catalog = await container.registry.planner_catalog(
        environment=environment or container.settings.environment,
        roles=identity.user.roles,
    )
    return {"environment": environment or container.settings.environment, "agents": catalog, "total": len(catalog)}


@router.post("/discover", response_model=AgentCreateResponse)
async def discover_and_register(
    payload: DiscoverRequest,
    container: AppContainer = Depends(get_container),
    _: IdentityContext = Depends(require_registry_admin),
) -> AgentCreateResponse:
    adapter = container.adapters.get("a2a")
    card = await container.registry.import_from_a2a(
        endpoint=payload.endpoint,
        adapter=adapter,
        environment=payload.environment or container.settings.environment,
        owner_override=payload.owner,
        risk_level=payload.risk_level,
        extra_tags=payload.tags,
    )
    return AgentCreateResponse(agent=card, created=True)


@router.get("/{agent_id}", response_model=AgentCard)
async def get_agent(
    agent_id: str,
    container: AppContainer = Depends(get_container),
    _: IdentityContext = Depends(get_identity),
) -> AgentCard:
    return await container.registry.get(agent_id)


@router.patch("/{agent_id}", response_model=AgentCard)
async def update_agent(
    agent_id: str,
    payload: AgentCardUpdate,
    container: AppContainer = Depends(get_container),
    _: IdentityContext = Depends(require_registry_admin),
) -> AgentCard:
    return await container.registry.update(agent_id, payload.model_dump(exclude_unset=True))


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_agent(
    agent_id: str,
    container: AppContainer = Depends(get_container),
    _: IdentityContext = Depends(require_registry_admin),
) -> Response:
    await container.registry.deactivate(agent_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{agent_id}/health", response_model=HealthResponse)
async def check_agent_health(
    agent_id: str,
    container: AppContainer = Depends(get_container),
    _: IdentityContext = Depends(get_identity),
) -> HealthResponse:
    card = await container.registry.get(agent_id)
    adapter = container.adapters.get(card.protocol)
    health = await container.registry.refresh_health(agent_id, adapter)
    return HealthResponse(agent_id=agent_id, health=str(health))
