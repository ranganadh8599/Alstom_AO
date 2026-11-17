"""Repositories: the only place that knows about SQL.

Each repository takes an ``AsyncSession`` and returns domain models, so services
and the orchestrator never depend on SQLAlchemy directly.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from alstom_ao.db.base import ensure_utc, utcnow
from alstom_ao.db.tables import AgentCardRow, ApprovalRow, PolicyDecisionRow, RunRow, TraceEventRow
from alstom_ao.domain.enums import AgentStatus, ApprovalStatus, RunStatus
from alstom_ao.domain.models import (
    AgentCard,
    ApprovalRequest,
    IdentityContext,
    Plan,
    PolicyDecision,
    RunRecord,
    TraceEvent,
)
from alstom_ao.errors import ConflictError, NotFoundError

# ---------------------------------------------------------------------------
# mappers
# ---------------------------------------------------------------------------


def agent_row_to_model(row: AgentCardRow) -> AgentCard:
    return AgentCard(
        agent_id=row.agent_id,
        name=row.name,
        description=row.description,
        platform=row.platform,
        protocol=row.protocol,
        endpoint=row.endpoint,
        owner=row.owner,
        owner_contact=row.owner_contact,
        capabilities=list(row.capabilities or []),
        capability_details=list(row.capability_details or []),
        input_schema=dict(row.input_schema or {}),
        output_schema=dict(row.output_schema or {}),
        auth=dict(row.auth or {}),
        permissions=dict(row.permissions or {}),
        version=row.version,
        status=row.status,
        environment=row.environment,
        risk_level=row.risk_level,
        tags=list(row.tags or []),
        examples=list(row.examples or []),
        health=row.health,
        metadata=dict(row.metadata_json or {}),
        created_at=ensure_utc(row.created_at),
        updated_at=ensure_utc(row.updated_at),
    )


def agent_model_to_row(card: AgentCard) -> AgentCardRow:
    return AgentCardRow(
        agent_id=card.agent_id,
        name=card.name,
        description=card.description,
        platform=card.platform,
        protocol=str(card.protocol),
        endpoint=card.endpoint,
        owner=card.owner,
        owner_contact=card.owner_contact,
        capabilities=card.capabilities,
        capability_details=[detail.model_dump(mode="json") for detail in card.capability_details],
        input_schema=card.input_schema,
        output_schema=card.output_schema,
        auth=card.auth.model_dump(mode="json"),
        permissions=card.permissions.model_dump(mode="json"),
        version=card.version,
        status=str(card.status),
        environment=card.environment,
        risk_level=str(card.risk_level),
        tags=card.tags,
        examples=card.examples,
        health=str(card.health),
        metadata_json=card.metadata,
    )


def trace_row_to_model(row: TraceEventRow) -> TraceEvent:
    return TraceEvent(
        run_id=row.run_id,
        trace_id=row.trace_id,
        span_id=row.span_id,
        parent_span_id=row.parent_span_id,
        sequence=row.sequence,
        agent_id=row.agent_id,
        agent_version=row.agent_version,
        protocol=row.protocol,
        action=row.action,
        status=row.status,
        initiated_by=row.initiated_by,
        authenticated_service=row.authenticated_service,
        use_case=row.use_case,
        timestamp=ensure_utc(row.timestamp) or utcnow(),
        input=row.input,
        output=row.output,
        latency_ms=row.latency_ms,
        retry_count=row.retry_count,
        error=row.error,
        metadata=dict(row.metadata_json or {}),
    )


def run_row_to_model(row: RunRow) -> RunRecord:
    return RunRecord(
        run_id=row.run_id,
        workflow_id=row.workflow_id,
        trace_id=row.trace_id,
        status=row.status,
        query=row.query,
        environment=row.environment,
        use_case=row.use_case,
        identity=IdentityContext.model_validate(row.identity),
        plan=Plan.model_validate(row.plan) if row.plan else None,
        selected_agents=list(row.selected_agents or []),
        response=row.response,
        error=row.error,
        approval_id=row.approval_id,
        usage=dict(row.usage or {}),
        langfuse_url=row.langfuse_url,
        created_at=ensure_utc(row.created_at) or utcnow(),
        updated_at=ensure_utc(row.updated_at) or utcnow(),
        completed_at=ensure_utc(row.completed_at),
    )


def approval_row_to_model(row: ApprovalRow) -> ApprovalRequest:
    return ApprovalRequest(
        approval_id=row.approval_id,
        run_id=row.run_id,
        agent_ids=list(row.agent_ids or []),
        action=row.action,
        risk_level=row.risk_level,
        reasons=list(row.reasons or []),
        status=row.status,
        requested_at=ensure_utc(row.requested_at) or utcnow(),
        expires_at=ensure_utc(row.expires_at),
        decided_at=ensure_utc(row.decided_at),
        decided_by=row.decided_by,
        decision_note=row.decision_note,
        payload=dict(row.payload or {}),
    )


# ---------------------------------------------------------------------------
# agents
# ---------------------------------------------------------------------------


class AgentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, card: AgentCard) -> AgentCard:
        self.session.add(agent_model_to_row(card))
        try:
            await self.session.flush()
        except IntegrityError as exc:
            await self.session.rollback()
            raise ConflictError(
                f"Agent '{card.agent_id}' already exists, or name/version/environment '{card.name}@{card.version}/{card.environment}' is taken.",
                details=str(exc.orig),
            ) from exc
        return card

    async def upsert(self, card: AgentCard) -> tuple[AgentCard, bool]:
        existing = await self.get(card.agent_id)
        if not existing:
            return await self.create(card), True
        merged = existing.model_copy(
            update={
                "name": card.name,
                "description": card.description,
                "platform": card.platform,
                "protocol": card.protocol,
                "endpoint": card.endpoint,
                "owner": card.owner,
                "owner_contact": card.owner_contact,
                "capabilities": card.capabilities,
                "capability_details": card.capability_details,
                "input_schema": card.input_schema,
                "output_schema": card.output_schema,
                "auth": card.auth,
                "permissions": card.permissions,
                "version": card.version,
                "status": card.status,
                "environment": card.environment,
                "risk_level": card.risk_level,
                "tags": card.tags,
                "examples": card.examples,
                "metadata": card.metadata,
            }
        )
        return await self.update(card.agent_id, merged), False

    async def get(self, agent_id: str) -> AgentCard | None:
        row = await self.session.get(AgentCardRow, agent_id)
        return agent_row_to_model(row) if row else None

    async def get_or_raise(self, agent_id: str) -> AgentCard:
        card = await self.get(agent_id)
        if not card:
            raise NotFoundError(f"Agent '{agent_id}' is not registered.")
        return card

    async def list(
        self,
        *,
        environment: str | None = None,
        status: AgentStatus | str | None = None,
        protocol: str | None = None,
        capability: str | None = None,
        query: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[AgentCard]:
        stmt = select(AgentCardRow)
        if environment:
            stmt = stmt.where(AgentCardRow.environment == environment)
        if status:
            stmt = stmt.where(AgentCardRow.status == str(status))
        if protocol:
            stmt = stmt.where(AgentCardRow.protocol == protocol)
        if query:
            like = f"%{query}%"
            stmt = stmt.where(or_(AgentCardRow.name.ilike(like), AgentCardRow.description.ilike(like)))
        stmt = stmt.order_by(AgentCardRow.name).limit(limit).offset(offset)
        rows = (await self.session.execute(stmt)).scalars().all()
        cards = [agent_row_to_model(row) for row in rows]
        if capability:
            needle = capability.lower()
            cards = [
                card
                for card in cards
                if any(needle in item.lower() for item in [*card.capabilities, *card.tags])
            ]
        return cards

    async def update(self, agent_id: str, card: AgentCard) -> AgentCard:
        row = await self.session.get(AgentCardRow, agent_id)
        if not row:
            raise NotFoundError(f"Agent '{agent_id}' is not registered.")
        row.name = card.name
        row.description = card.description
        row.platform = card.platform
        row.protocol = str(card.protocol)
        row.endpoint = card.endpoint
        row.owner = card.owner
        row.owner_contact = card.owner_contact
        row.capabilities = card.capabilities
        row.capability_details = [detail.model_dump(mode="json") for detail in card.capability_details]
        row.input_schema = card.input_schema
        row.output_schema = card.output_schema
        row.auth = card.auth.model_dump(mode="json")
        row.permissions = card.permissions.model_dump(mode="json")
        row.version = card.version
        row.status = str(card.status)
        row.environment = card.environment
        row.risk_level = str(card.risk_level)
        row.tags = card.tags
        row.examples = card.examples
        row.metadata_json = card.metadata
        row.updated_at = utcnow()
        try:
            await self.session.flush()
        except IntegrityError as exc:
            await self.session.rollback()
            raise ConflictError("Update violates a uniqueness constraint.", details=str(exc.orig)) from exc
        return agent_row_to_model(row)

    async def set_status(self, agent_id: str, status: AgentStatus) -> None:
        await self._patch(agent_id, status=str(status))

    async def set_health(self, agent_id: str, health: str) -> None:
        await self._patch(agent_id, health=health, last_health_check_at=utcnow())

    async def _patch(self, agent_id: str, **values: Any) -> None:
        stmt = update(AgentCardRow).where(AgentCardRow.agent_id == agent_id).values(**values)
        result = await self.session.execute(stmt)
        if result.rowcount == 0:
            raise NotFoundError(f"Agent '{agent_id}' is not registered.")

    async def delete(self, agent_id: str) -> None:
        row = await self.session.get(AgentCardRow, agent_id)
        if not row:
            raise NotFoundError(f"Agent '{agent_id}' is not registered.")
        await self.session.delete(row)

    async def count(self) -> int:
        return int((await self.session.execute(select(func.count()).select_from(AgentCardRow))).scalar_one())


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------


class RunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, run: RunRecord) -> RunRecord:
        self.session.add(
            RunRow(
                run_id=run.run_id,
                workflow_id=run.workflow_id,
                trace_id=run.trace_id,
                status=str(run.status),
                query=run.query,
                environment=run.environment,
                use_case=run.use_case,
                user_id=run.identity.user.user_id,
                tenant_id=run.identity.user.tenant_id,
                identity=run.identity.model_dump(mode="json"),
                plan=run.plan.model_dump(mode="json") if run.plan else {},
                selected_agents=run.selected_agents,
                response=run.response,
                error=run.error,
                approval_id=run.approval_id,
                usage=run.usage,
                langfuse_url=run.langfuse_url,
            )
        )
        await self.session.flush()
        return run

    async def get(self, run_id: str) -> RunRecord | None:
        row = await self.session.get(RunRow, run_id)
        return run_row_to_model(row) if row else None

    async def get_or_raise(self, run_id: str) -> RunRecord:
        run = await self.get(run_id)
        if not run:
            raise NotFoundError(f"Run '{run_id}' does not exist.")
        return run

    async def save_state(
        self,
        run_id: str,
        *,
        status: RunStatus | None = None,
        plan: Plan | None = None,
        selected_agents: list[str] | None = None,
        response: str | None = None,
        error: str | None = None,
        approval_id: str | None = None,
        usage: dict[str, Any] | None = None,
        langfuse_url: str | None = None,
        completed: bool = False,
    ) -> None:
        values: dict[str, Any] = {"updated_at": utcnow()}
        if status is not None:
            values["status"] = str(status)
        if plan is not None:
            values["plan"] = plan.model_dump(mode="json")
        if selected_agents is not None:
            values["selected_agents"] = selected_agents
        if response is not None:
            values["response"] = response
        if error is not None:
            values["error"] = error
        if approval_id is not None:
            values["approval_id"] = approval_id
        if usage is not None:
            values["usage"] = usage
        if langfuse_url is not None:
            values["langfuse_url"] = langfuse_url
        if completed:
            values["completed_at"] = utcnow()
        stmt = update(RunRow).where(RunRow.run_id == run_id).values(**values)
        result = await self.session.execute(stmt)
        if result.rowcount == 0:
            raise NotFoundError(f"Run '{run_id}' does not exist.")

    async def list_recent(
        self,
        *,
        user_id: str | None = None,
        tenant_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RunRecord]:
        stmt = select(RunRow)
        if user_id:
            stmt = stmt.where(RunRow.user_id == user_id)
        if tenant_id:
            stmt = stmt.where(RunRow.tenant_id == tenant_id)
        if status:
            stmt = stmt.where(RunRow.status == status)
        stmt = stmt.order_by(RunRow.created_at.desc()).limit(limit).offset(offset)
        rows = (await self.session.execute(stmt)).scalars().all()
        return [run_row_to_model(row) for row in rows]

    async def count(self, status: str | None = None) -> int:
        stmt = select(func.count()).select_from(RunRow)
        if status:
            stmt = stmt.where(RunRow.status == status)
        return int((await self.session.execute(stmt)).scalar_one())

    async def awaiting_approval_count(self) -> int:
        return await self.count(status=str(RunStatus.AWAITING_APPROVAL))


# ---------------------------------------------------------------------------
# trace ledger
# ---------------------------------------------------------------------------


class TraceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record_many(self, events: list[TraceEvent]) -> None:
        if not events:
            return
        self.session.add_all(
            [
                TraceEventRow(
                    run_id=event.run_id,
                    trace_id=event.trace_id,
                    span_id=event.span_id,
                    parent_span_id=event.parent_span_id,
                    sequence=event.sequence,
                    agent_id=event.agent_id,
                    agent_version=event.agent_version,
                    protocol=event.protocol,
                    action=event.action,
                    status=str(event.status),
                    initiated_by=event.initiated_by,
                    authenticated_service=event.authenticated_service,
                    use_case=event.use_case,
                    timestamp=event.timestamp,
                    input=event.input,
                    output=event.output,
                    latency_ms=event.latency_ms,
                    retry_count=event.retry_count,
                    error=event.error,
                    metadata_json=event.metadata,
                )
                for event in events
            ]
        )
        await self.session.flush()

    async def list_for_run(self, run_id: str, *, limit: int = 500) -> list[TraceEvent]:
        stmt = (
            select(TraceEventRow)
            .where(TraceEventRow.run_id == run_id)
            .order_by(TraceEventRow.event_id.asc())
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return [trace_row_to_model(row) for row in rows]

    async def count(self) -> int:
        return int((await self.session.execute(select(func.count()).select_from(TraceEventRow))).scalar_one())


# ---------------------------------------------------------------------------
# approvals
# ---------------------------------------------------------------------------


class ApprovalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, request: ApprovalRequest) -> ApprovalRequest:
        self.session.add(
            ApprovalRow(
                approval_id=request.approval_id,
                run_id=request.run_id,
                agent_ids=request.agent_ids,
                action=str(request.action),
                risk_level=str(request.risk_level),
                reasons=request.reasons,
                status=str(request.status),
                requested_at=request.requested_at,
                expires_at=request.expires_at,
                payload=request.payload,
            )
        )
        try:
            await self.session.flush()
        except IntegrityError as exc:
            await self.session.rollback()
            raise ConflictError(f"Run '{request.run_id}' already has an approval request.") from exc
        return request

    async def get(self, approval_id: str) -> ApprovalRequest | None:
        row = await self.session.get(ApprovalRow, approval_id)
        return approval_row_to_model(row) if row else None

    async def get_by_run(self, run_id: str) -> ApprovalRequest | None:
        row = (
            await self.session.execute(select(ApprovalRow).where(ApprovalRow.run_id == run_id))
        ).scalar_one_or_none()
        return approval_row_to_model(row) if row else None

    async def decide(
        self,
        approval_id: str,
        *,
        approved: bool,
        decided_by: str,
        note: str | None = None,
    ) -> ApprovalRequest:
        row = await self.session.get(ApprovalRow, approval_id)
        if not row:
            raise NotFoundError(f"Approval '{approval_id}' does not exist.")
        if row.status != str(ApprovalStatus.PENDING):
            return approval_row_to_model(row)
        row.status = str(ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED)
        row.decided_at = utcnow()
        row.decided_by = decided_by
        row.decision_note = note
        await self.session.flush()
        return approval_row_to_model(row)

    async def expire(self, approval_id: str) -> None:
        stmt = (
            update(ApprovalRow)
            .where(ApprovalRow.approval_id == approval_id, ApprovalRow.status == str(ApprovalStatus.PENDING))
            .values(status=str(ApprovalStatus.EXPIRED), decided_at=utcnow(), decision_note="expired")
        )
        await self.session.execute(stmt)

    async def list_pending(self, *, limit: int = 100) -> list[ApprovalRequest]:
        stmt = (
            select(ApprovalRow)
            .where(ApprovalRow.status == str(ApprovalStatus.PENDING))
            .order_by(ApprovalRow.requested_at.asc())
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return [approval_row_to_model(row) for row in rows]


# ---------------------------------------------------------------------------
# policy decisions
# ---------------------------------------------------------------------------


class PolicyDecisionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record_many(self, run_id: str, decisions: list[PolicyDecision]) -> None:
        if not decisions:
            return
        self.session.add_all(
            [
                PolicyDecisionRow(
                    run_id=run_id,
                    agent_id=decision.agent_id,
                    action=str(decision.action),
                    allowed=decision.allowed,
                    source=decision.source,
                    reasons=decision.reasons,
                    obligations=decision.obligations,
                )
                for decision in decisions
            ]
        )
        await self.session.flush()

    async def list_for_run(self, run_id: str) -> list[PolicyDecision]:
        stmt = (
            select(PolicyDecisionRow)
            .where(PolicyDecisionRow.run_id == run_id)
            .order_by(PolicyDecisionRow.decision_id.asc())
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return [
            PolicyDecision(
                allowed=row.allowed,
                reasons=list(row.reasons or []),
                obligations=dict(row.obligations or {}),
                source=row.source,
                agent_id=row.agent_id,
                action=row.action,
            )
            for row in rows
        ]


def is_expired(expires_at: datetime | None, *, now: datetime | None = None) -> bool:
    if not expires_at:
        return False
    current = now or utcnow()
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=current.tzinfo)
    return expires_at <= current
