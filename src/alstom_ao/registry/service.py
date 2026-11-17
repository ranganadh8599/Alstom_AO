"""Agent registry: the central catalog of who exists and what they can do.

Services open a short-lived session per operation; repositories are never shared
across requests.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import ValidationError

from alstom_ao.adapters.base import AgentAdapter
from alstom_ao.config import Settings
from alstom_ao.db.repositories import AgentRepository
from alstom_ao.db.session import Database
from alstom_ao.domain.enums import AgentHealth, AgentStatus, RiskLevel
from alstom_ao.domain.models import AgentCard, AgentCardCreate, AgentPermissions
from alstom_ao.errors import InputValidationError, NotFoundError
from alstom_ao.llm.schema_check import validate_schema_definition

logger = logging.getLogger(__name__)


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "agent"


class AgentRegistryService:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings

    async def register(self, payload: AgentCardCreate) -> AgentCard:
        self._check_schema(payload)
        try:
            card = AgentCard(**payload.model_dump())
        except ValidationError as exc:
            raise InputValidationError("The agent card is not valid.", details=exc.errors()) from exc
        async with self.database.session() as session:
            return await AgentRepository(session).create(card)

    async def upsert(self, payload: AgentCardCreate) -> tuple[AgentCard, bool]:
        self._check_schema(payload)
        try:
            card = AgentCard(**payload.model_dump())
        except ValidationError as exc:
            raise InputValidationError("The agent card is not valid.", details=exc.errors()) from exc
        async with self.database.session() as session:
            return await AgentRepository(session).upsert(card)

    def _check_schema(self, payload: AgentCardCreate) -> None:
        """Reject a bad ``output_schema`` when the card is submitted.

        Doing this at registration is the whole point: the administrator gets one
        clear error, and runtime validation never has to resolve a remote ``$ref``
        (which would make the result depend on a network fetch).
        """

        verdict = validate_schema_definition(
            payload.output_schema,
            allow_remote_refs=self.settings.allow_remote_schema_refs,
        )
        if verdict.status == "schema_invalid":
            raise InputValidationError(
                "output_schema is not valid: " + "; ".join(verdict.errors[:3]),
                details={"errors": verdict.errors},
            )

    async def get(self, agent_id: str) -> AgentCard:
        async with self.database.session() as session:
            return await AgentRepository(session).get_or_raise(agent_id)

    async def list_cards(self, **filters: Any) -> list[AgentCard]:
        """List cards. Named ``list_cards`` because a method called ``list``
        shadows the builtin inside the class body and breaks annotation
        introspection."""

        async with self.database.session() as session:
            return await AgentRepository(session).list(**filters)

    async def update(self, agent_id: str, patch: dict[str, Any]) -> AgentCard:
        async with self.database.session() as session:
            repo = AgentRepository(session)
            current = await repo.get_or_raise(agent_id)
            merged = current.model_copy(update={key: value for key, value in patch.items() if value is not None})
            # Re-validate: a patch must not be able to produce an invalid card.
            validated = AgentCard(**merged.model_dump())
            return await repo.update(agent_id, validated)

    async def deactivate(self, agent_id: str) -> None:
        async with self.database.session() as session:
            await AgentRepository(session).set_status(agent_id, AgentStatus.INACTIVE)

    async def refresh_health(self, agent_id: str, adapter: AgentAdapter) -> AgentHealth:
        card = await self.get(agent_id)
        health = await adapter.check_health(card)
        async with self.database.session() as session:
            await AgentRepository(session).set_health(agent_id, str(health))
        return health

    async def count(self) -> int:
        async with self.database.session() as session:
            return await AgentRepository(session).count()

    # -- A2A discovery / import -------------------------------------------
    async def import_from_a2a(
        self,
        *,
        endpoint: str,
        adapter: AgentAdapter,
        environment: str,
        owner_override: str | None = None,
        risk_level: RiskLevel = RiskLevel.LOW,
        extra_tags: list[str] | None = None,
    ) -> AgentCard:
        document = await adapter.discover(endpoint)
        if not document:
            raise NotFoundError(
                "No A2A agent card found at the endpoint.",
                details=f"Tried {self.settings.a2a_discovery_path} and {self.settings.a2a_legacy_discovery_path}.",
            )

        provider = document.get("provider") or {}
        metadata = document.get("metadata") or {}
        skills = document.get("skills") or []
        capability_names: list[str] = []
        tags: list[str] = list(extra_tags or [])
        examples: list[str] = []
        for skill in skills:
            if not isinstance(skill, dict):
                continue
            name = skill.get("name") or skill.get("id")
            if name:
                capability_names.append(str(name))
            tags.extend(str(tag) for tag in skill.get("tags") or [])
            examples.extend(str(example) for example in skill.get("examples") or [])

        if not capability_names:
            capability_names = [str(item) for item in document.get("defaultInputModes") or []]

        agent_id = str(metadata.get("alstom_agent_id") or slugify(document.get("name", "agent")))
        payload = AgentCardCreate(
            agent_id=agent_id,
            name=document.get("name") or agent_id,
            description=document.get("description") or "Discovered A2A agent",
            platform=str(metadata.get("platform") or "a2a"),
            protocol="a2a",
            endpoint=str(document.get("url") or endpoint),
            owner=owner_override or str(provider.get("organization") or ""),
            owner_contact=provider.get("url"),
            capabilities=capability_names or ["general"],
            input_schema=metadata.get("input_schema") or {},
            output_schema=metadata.get("output_schema") or {},
            permissions=AgentPermissions(
                allowed_roles=list(metadata.get("allowed_roles") or []),
                allowed_actions=list(metadata.get("allowed_actions") or []),
                requires_approval_for=list(metadata.get("requires_approval_for") or []),
            ),
            version=str(document.get("version") or "1.0.0"),
            status=AgentStatus.ACTIVE,
            environment=str(metadata.get("environment") or environment),
            risk_level=str(metadata.get("alstom_risk_level") or risk_level),
            tags=tags,
            examples=examples[:10],
            metadata={
                **metadata,
                "discovered_from": endpoint,
                "raw_card_keys": sorted(document.keys()),
            },
        )
        card, _ = await self.upsert(payload)
        return card

    # -- planner views -----------------------------------------------------
    async def planner_catalog(
        self,
        *,
        environment: str,
        roles: list[str] | None = None,
        include_degraded: bool = True,
    ) -> list[dict[str, Any]]:
        """Compact, safe catalog for the planner prompt (no endpoints, no auth).

        Roles shape the prompt so a user is not offered agents they can never call.
        But if role filtering would hide *every* agent, the result is a confusing
        "no agents registered" message for a user who merely has an unmapped role
        (e.g. a UI sending ``ADMIN`` instead of a business role). In that case the
        catalog is returned unfiltered and OPA denies per agent with a real reason,
        which is both accurate and diagnosable.
        """

        cards = await self.list_cards(environment=environment, status=AgentStatus.ACTIVE, limit=500)
        role_set = {role.lower() for role in (roles or [])}
        catalog: list[dict[str, Any]] = []
        role_filtered_out = 0
        for card in cards:
            if card.health == AgentHealth.UNHEALTHY:
                continue
            if not include_degraded and card.health == AgentHealth.DEGRADED:
                continue
            if card.permissions.allowed_roles and role_set:
                allowed = {role.lower() for role in card.permissions.allowed_roles}
                if not (allowed & role_set) and "admin" not in role_set:
                    role_filtered_out += 1
                    continue
            catalog.append(self._catalog_entry(card))

        if not catalog and role_filtered_out:
            logger.warning(
                "Role filter %s excluded all %s agent(s); returning the unfiltered catalog and letting policy decide.",
                sorted(role_set),
                role_filtered_out,
            )
            catalog = [
                self._catalog_entry(card)
                for card in cards
                if card.health != AgentHealth.UNHEALTHY
                and (include_degraded or card.health != AgentHealth.DEGRADED)
            ]
        return catalog

    @staticmethod
    def _catalog_entry(card: AgentCard) -> dict[str, Any]:
        return {
            "agent_id": card.agent_id,
            "name": card.name,
            "description": card.description,
            "capabilities": card.capabilities,
            "tags": card.tags,
            "examples": card.examples[:3],
            "risk_level": str(card.risk_level),
            "allowed_actions": [str(action) for action in card.permissions.allowed_actions],
            "requires_approval_for": [str(action) for action in card.permissions.requires_approval_for],
            "version": card.version,
            "owner": card.owner,
        }

    @staticmethod
    def catalog_summary(catalog: list[dict[str, Any]], limit: int = 6) -> str:
        if not catalog:
            return "no agents are registered for this environment"
        parts = []
        for item in catalog[:limit]:
            capability = ", ".join(item["capabilities"][:3])
            parts.append(f"{item['name']} ({capability})")
        return "; ".join(parts)


async def seed_demo_agents(service: AgentRegistryService, settings: Settings) -> int:
    """Idempotently register demo cards so a fresh install is usable immediately."""

    from alstom_ao.registry.demo_cards import demo_cards

    created = 0
    for payload in demo_cards(settings):
        try:
            await service.get(payload.agent_id)
            continue
        except NotFoundError:
            pass
        await service.upsert(payload)
        created += 1
    return created
