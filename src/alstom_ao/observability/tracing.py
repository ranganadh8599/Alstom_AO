"""Langfuse tracing + a durable local ledger.

Two complementary mechanisms:

1. **Langfuse** gets one trace per run, with a span per orchestration node and an
   ``agent`` observation per agent invocation. The full common trace context
   (workflow, trace, agent, version, protocol, initiator, service, use case,
   action, status, retries) is attached to every span.
2. **trace_events** (Postgres) is the append-only operational ledger. It answers
   "who did what, on whose behalf, with which inputs/outputs, after which other
   agents" even when Langfuse is unreachable, and it is what the API exposes.

Tracing must never break a run: every Langfuse interaction is best-effort.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from alstom_ao.config import Settings
from alstom_ao.db.repositories import TraceRepository
from alstom_ao.db.session import Database
from alstom_ao.domain.enums import ActionType, TraceStatus
from alstom_ao.domain.models import IdentityContext, TraceEvent

logger = logging.getLogger(__name__)


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex}"


def new_trace_id() -> str:
    # Langfuse/W3C trace ids are 32 lowercase hex characters.
    return uuid.uuid4().hex


def _safe(value: Any) -> Any:
    """Make arbitrary values JSON-serializable without leaking non-serializable objects."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe(item) for item in value]
    return str(value)


class TraceLedger(Protocol):
    async def record_many(self, events: list[TraceEvent]) -> None: ...


class DBTraceLedger:
    """Persists trace events using short-lived sessions (append-only)."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def record_many(self, events: list[TraceEvent]) -> None:
        if not events:
            return
        try:
            async with self.database.session() as session:
                await TraceRepository(session).record_many(events)
        except Exception as exc:  # ledger failure must not break the run
            logger.error("Failed to persist %s trace event(s): %s", len(events), exc)


class NullLedger:
    async def record_many(self, events: list[TraceEvent]) -> None:
        return None


@dataclass
class Observation:
    span_id: str
    _langfuse_span: Any | None = None
    _closed: bool = False

    def update(self, **kwargs: Any) -> None:
        if self._langfuse_span is None or self._closed:
            return
        payload = {key: _safe(value) for key, value in kwargs.items() if value is not None}
        if not payload:
            return
        try:
            self._langfuse_span.update(**payload)
        except Exception as exc:  # pragma: no cover - best effort
            logger.debug("Langfuse span update failed: %s", exc)


def _build_langfuse_client(settings: Settings):
    if not settings.langfuse_enabled:
        return None
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        logger.warning("Langfuse is enabled but keys are missing; tracing disabled.")
        return None
    try:
        from langfuse import Langfuse

        return Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
            tracing_enabled=True,
            sample_rate=settings.trace_sample_rate,
            environment=settings.environment,
            release="alstom-ao@0.1.0",
        )
    except Exception as exc:  # pragma: no cover - optional dependency
        logger.warning("Langfuse client could not be created: %s", exc)
        return None


class Tracer:
    """Run-scoped tracer. One instance per run."""

    def __init__(
        self,
        *,
        settings: Settings,
        ledger: TraceLedger,
        client: Any | None,
        run_id: str,
        workflow_id: str,
        trace_id: str,
        identity: IdentityContext,
        use_case: str,
        environment: str,
    ) -> None:
        self.settings = settings
        self.ledger = ledger
        self.client = client
        self.run_id = run_id
        self.workflow_id = workflow_id
        self.trace_id = trace_id
        self.identity = identity
        self.use_case = use_case
        self.environment = environment
        self._sequence = 0

    # -- identifiers -------------------------------------------------------
    @property
    def initiated_by(self) -> str:
        return self.identity.user.user_id

    @property
    def authenticated_service(self) -> str:
        return self.identity.service.service_id

    @property
    def langfuse_url(self) -> str | None:
        if not self.client:
            return None
        try:
            return self.client.get_trace_url(trace_id=self.trace_id)
        except Exception:
            return None

    def next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    # -- ledger ------------------------------------------------------------
    async def record(
        self,
        action: str,
        status: TraceStatus,
        *,
        agent_id: str | None = None,
        agent_version: str | None = None,
        protocol: str | None = None,
        use_case: str | None = None,
        span_id: str | None = None,
        parent_span_id: str | None = None,
        input: Any = None,
        output: Any = None,
        latency_ms: int | None = None,
        retry_count: int = 0,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
        queue: list[TraceEvent] | None = None,
    ) -> TraceEvent:
        event = TraceEvent(
            run_id=self.run_id,
            trace_id=self.trace_id,
            span_id=span_id or uuid.uuid4().hex[:16],
            parent_span_id=parent_span_id,
            sequence=self.next_sequence(),
            agent_id=agent_id,
            agent_version=agent_version,
            protocol=protocol,
            action=str(action),
            status=status,
            initiated_by=self.initiated_by,
            authenticated_service=self.authenticated_service,
            use_case=use_case or self.use_case,
            timestamp=datetime.now(UTC),
            input=_safe(input),
            output=_safe(output),
            latency_ms=latency_ms,
            retry_count=retry_count,
            error=error,
            metadata=_safe(metadata or {}),
        )
        if queue is not None:
            queue.append(event)
        else:
            await self.ledger.record_many([event])
        return event

    async def flush(self, events: list[TraceEvent]) -> None:
        await self.ledger.record_many(events)

    # -- Langfuse spans ----------------------------------------------------
    @asynccontextmanager
    async def span(
        self,
        name: str,
        *,
        as_type: str = "span",
        input: Any = None,
        metadata: dict[str, Any] | None = None,
        agent_id: str | None = None,
        agent_version: str | None = None,
        protocol: str | None = None,
        action: ActionType | None = None,
    ) -> AsyncIterator[Observation]:
        span_id = uuid.uuid4().hex[:16]
        observation = Observation(span_id=span_id)
        cm = None
        kwargs: dict[str, Any] = {
            "name": name,
            "as_type": as_type,
            "input": _safe(input),
            "metadata": _safe(
                {
                    "workflow_id": self.workflow_id,
                    "run_id": self.run_id,
                    "trace_id": self.trace_id,
                    "agent_id": agent_id,
                    "agent_version": agent_version,
                    "protocol": protocol,
                    "action": str(action) if action else None,
                    "initiated_by": self.initiated_by,
                    "authenticated_service": self.authenticated_service,
                    "use_case": self.use_case,
                    "environment": self.environment,
                    **(metadata or {}),
                }
            ),
            "version": agent_version,
        }
        if self.client is not None:
            try:
                cm = self.client.start_as_current_observation(
                    trace_context={"trace_id": self.trace_id},
                    **kwargs,
                )
                observation._langfuse_span = cm.__enter__()
            except Exception as exc:  # pragma: no cover - best effort
                logger.debug("Langfuse span could not be started: %s", exc)
                cm = None

        try:
            yield observation
        except Exception as exc:
            observation.update(level="ERROR", status_message=str(exc)[:500])
            raise
        finally:
            observation._closed = True
            if cm is not None:
                try:
                    cm.__exit__(None, None, None)
                except Exception:  # pragma: no cover - best effort
                    pass


class TracingService:
    """Creates run-scoped tracers and owns the Langfuse client lifecycle."""

    def __init__(self, settings: Settings, database: Database | None = None) -> None:
        self.settings = settings
        self.ledger: TraceLedger = DBTraceLedger(database) if database else NullLedger()
        self.client = _build_langfuse_client(settings)

    def tracer(
        self,
        *,
        run_id: str,
        workflow_id: str,
        trace_id: str,
        identity: IdentityContext,
        use_case: str,
        environment: str,
    ) -> Tracer:
        return Tracer(
            settings=self.settings,
            ledger=self.ledger,
            client=self.client,
            run_id=run_id,
            workflow_id=workflow_id,
            trace_id=trace_id,
            identity=identity,
            use_case=use_case,
            environment=environment,
        )

    async def shutdown(self) -> None:
        if self.client is not None:
            try:
                await asyncio.to_thread(self.client.flush)
                self.client.shutdown()
            except Exception:  # pragma: no cover - best effort
                pass
