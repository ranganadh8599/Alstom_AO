"""Run lifecycle: start, pause for approval, resume, and persist the outcome.

The run record is written **before** the graph starts, so a crash never leaves an
invisible run. Every terminal path (completed, no match, denied, rejected,
invalid, failed, timeout) is persisted with a user-safe response.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from langgraph.types import Command

from alstom_ao.config import Settings
from alstom_ao.db.repositories import PolicyDecisionRepository, RunRepository, TraceRepository
from alstom_ao.db.session import Database
from alstom_ao.domain.enums import RunStatus, TraceAction, TraceStatus
from alstom_ao.domain.models import (
    ApprovalRequest,
    IdentityContext,
    InvocationResult,
    Plan,
    PolicyDecision,
    RunRecord,
    RunResult,
    TraceEvent,
)
from alstom_ao.errors import (
    AppError,
    BudgetExceededError,
    ConflictError,
    NotFoundError,
    UpstreamUnavailableError,
)
from alstom_ao.llm.gateway import LLMBudget, build_gateway
from alstom_ao.observability.metrics import metrics
from alstom_ao.observability.tracing import new_trace_id
from alstom_ao.orchestrator.runtime import NodeServices, RunRuntime

logger = logging.getLogger(__name__)

APPROVAL_REQUIRED_TEMPLATE = (
    "This request needs human approval before it can run.\n\n"
    "- **Approval ID:** `{approval_id}`\n"
    "- **Risk:** {risk}\n"
    "- **Why:** {reasons}\n"
    "- **Agents:** {agents}\n\n"
    "An approver can approve or reject it via `POST /v1/approvals/{approval_id}/decision`. "
    "Nothing has been executed yet."
)
TIMEOUT_MESSAGE = (
    "The request took too long and was stopped. No further agents were called. "
    "Please narrow the request or try again."
)
FAILED_MESSAGE = (
    "I couldn't complete this request because of an internal error. "
    "The failure has been traced; please try again or contact the platform team."
)
BUDGET_MESSAGE = (
    "This request exceeded the orchestration budget (model calls, tokens or cost). "
    "Please narrow the request or ask the platform team to raise the limit."
)
UPSTREAM_MESSAGE = (
    "The language model gateway is currently unavailable. Agent results were not summarised; "
    "please retry in a moment."
)


def _to_status(value: str | None, fallback: RunStatus = RunStatus.FAILED) -> RunStatus:
    try:
        return RunStatus(value) if value else fallback
    except ValueError:
        return fallback


class RunService:
    def __init__(self, services: NodeServices, graph: Any) -> None:
        self.services = services
        self.graph = graph

    @property
    def settings(self) -> Settings:
        return self.services.settings

    @property
    def database(self) -> Database:
        return self.services.database

    # -- read models -------------------------------------------------------
    async def get_run(self, run_id: str) -> RunRecord:
        async with self.database.session() as session:
            return await RunRepository(session).get_or_raise(run_id)

    async def get_trace(self, run_id: str) -> list[TraceEvent]:
        async with self.database.session() as session:
            return await TraceRepository(session).list_for_run(run_id)

    async def get_policy_decisions(self, run_id: str) -> list[PolicyDecision]:
        async with self.database.session() as session:
            return await PolicyDecisionRepository(session).list_for_run(run_id)

    async def _find_idempotent(self, user_id: str, idempotency_key: str) -> RunRecord | None:
        if not idempotency_key:
            return None
        async with self.database.session() as session:
            return await RunRepository(session).find_by_idempotency(
                user_id=user_id, idempotency_key=idempotency_key
            )

    async def _replay(self, record: RunRecord, started: float) -> RunResult:
        """Rebuild the response of an earlier run without executing anything."""

        events = await self.get_trace(record.run_id)
        decisions = await self.get_policy_decisions(record.run_id)
        approval = await self.services.approvals.get_by_run(record.run_id)

        # Reconstruct per-agent results from the ledger so the caller gets the
        # same payload shape as a live run.
        agent_results: list[InvocationResult] = []
        for event in events:
            if event.action != TraceAction.AGENT_COMPLETED or not event.agent_id:
                continue
            output = event.output or {}
            agent_results.append(
                InvocationResult(
                    agent_id=event.agent_id,
                    agent_name=str(output.get("agent_name") or event.agent_id),
                    agent_version=event.agent_version or "",
                    protocol=event.protocol or "a2a",
                    status=TraceStatus.SUCCEEDED,
                    text=str(output.get("text") or ""),
                    artifacts=list(output.get("artifacts") or []),
                    latency_ms=event.latency_ms or 0,
                    retry_count=event.retry_count,
                )
            )

        return RunResult(
            run_id=record.run_id,
            workflow_id=record.workflow_id,
            trace_id=record.trace_id,
            status=record.status,
            response=record.response,
            plan=record.plan,
            agent_results=agent_results,
            approval=approval,
            policy_decisions=decisions,
            usage=record.usage,
            langfuse_url=record.langfuse_url,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    async def list_runs(
        self,
        *,
        user_id: str | None = None,
        tenant_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RunRecord]:
        async with self.database.session() as session:
            return await RunRepository(session).list_recent(
                user_id=user_id, tenant_id=tenant_id, status=status, limit=limit, offset=offset
            )

    # -- write path --------------------------------------------------------
    async def start(
        self,
        *,
        query: str,
        identity: IdentityContext,
        environment: str | None = None,
        use_case: str = "general",
        conversation: list[dict[str, str]] | None = None,
        trace_id: str | None = None,
        workflow_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> RunResult:
        settings = self.settings
        started = time.perf_counter()

        # A retried submission must never start a second run: an approved write
        # would otherwise execute twice. Returning the first run's result makes
        # the endpoint idempotent.
        if idempotency_key:
            existing = await self._find_idempotent(identity.user.user_id, idempotency_key)
            if existing is not None:
                metrics.incr("ao.runs.idempotent_replay")
                return await self._replay(existing, started)

        run_id = f"run_{uuid.uuid4().hex[:24]}"
        workflow_id = workflow_id or f"wf_{uuid.uuid4().hex[:24]}"
        trace = trace_id if trace_id and len(trace_id) == 32 else new_trace_id()
        environment = environment or settings.environment

        record = RunRecord(
            run_id=run_id,
            workflow_id=workflow_id,
            trace_id=trace,
            status=RunStatus.PENDING,
            query=query,
            environment=environment,
            use_case=use_case,
            identity=identity,
            idempotency_key=idempotency_key,
        )
        try:
            async with self.database.session() as session:
                await RunRepository(session).create(record)
        except ConflictError:
            # Lost a race with a concurrent identical submission: reuse the winner.
            existing = await self._find_idempotent(identity.user.user_id, idempotency_key or "")
            if existing is None:
                raise
            metrics.incr("ao.runs.idempotent_replay")
            return await self._replay(existing, started)

        metrics.incr("ao.runs.started")
        runtime = self._build_runtime(
            run_id=run_id,
            workflow_id=workflow_id,
            trace_id=trace,
            identity=identity,
            environment=environment,
            use_case=use_case,
            usage={},
        )
        await runtime.tracer.record(
            TraceAction.RUN_STARTED,
            TraceStatus.STARTED,
            input={"query": query[:2000], "use_case": use_case, "environment": environment},
            metadata={
                "user": identity.user.model_dump(mode="json"),
                "service": identity.service.model_dump(mode="json"),
                "conversation_turns": len(conversation or []),
            },
        )

        state = {
            "run_id": run_id,
            "workflow_id": workflow_id,
            "trace_id": trace,
            "query": query,
            "environment": environment,
            "use_case": use_case,
            "conversation": conversation or [],
            "identity": identity.model_dump(mode="json"),
            "status": str(RunStatus.PLANNING),
            "results": [],
            "failures": [],
            "executed_agents": [],
            "usage": {},
        }
        return await self._invoke(
            run_id=run_id,
            runtime=runtime,
            payload=state,
            started=started,
            resume=False,
        )

    async def ensure_resumable(self, run_id: str) -> None:
        """Refuse to resume a run whose graph state is gone.

        Without a durable checkpointer, an in-flight pause lives only in memory.
        After a restart the approval row still exists, so approving would look
        successful while executing nothing. Fail loudly instead.
        """

        config = {"configurable": {"thread_id": run_id}}
        try:
            snapshot = await self.graph.aget_state(config)
        except Exception as exc:  # pragma: no cover - checkpointer edge cases
            raise ConflictError(
                "This run's orchestration state could not be read, so it cannot be resumed.",
                details=str(exc),
                code="run_state_unavailable",
            ) from exc

        if not snapshot or not (snapshot.values or {}):
            raise ConflictError(
                "This run's paused orchestration state is no longer available, so it cannot be resumed. "
                "Nothing was executed. Install a durable checkpointer (Postgres + the 'postgres' extra) "
                "so paused runs survive a restart.",
                code="run_state_unavailable",
            )

    async def resume(
        self,
        *,
        run_id: str,
        approved: bool,
        actor: IdentityContext,
        note: str | None = None,
    ) -> RunResult:
        record = await self.get_run(run_id)
        if record.status != RunStatus.AWAITING_APPROVAL:
            raise ConflictError(
                f"Run '{run_id}' is not awaiting approval (status: {record.status}).",
            )
        approval = await self.services.approvals.get_by_run(run_id)
        if approval is None:
            raise NotFoundError(f"Run '{run_id}' has no approval request.")
        await self.ensure_resumable(run_id)

        started = time.perf_counter()
        runtime = self._build_runtime(
            run_id=record.run_id,
            workflow_id=record.workflow_id,
            trace_id=record.trace_id,
            identity=record.identity,
            environment=record.environment,
            use_case=record.use_case,
            usage=record.usage,
        )
        metrics.incr("ao.approvals.resumed")
        return await self._invoke(
            run_id=run_id,
            runtime=runtime,
            payload=Command(
                resume={"approved": approved, "decided_by": actor.user.user_id, "note": note, "approval_id": approval.approval_id}
            ),
            started=started,
            resume=True,
        )

    # -- internals ---------------------------------------------------------
    def _build_runtime(
        self,
        *,
        run_id: str,
        workflow_id: str,
        trace_id: str,
        identity: IdentityContext,
        environment: str,
        use_case: str,
        usage: dict[str, Any],
    ) -> RunRuntime:
        budget = LLMBudget.from_snapshot(usage or {}, self.settings)
        gateway = build_gateway(self.settings, budget)
        tracer = self.services.tracing.tracer(
            run_id=run_id,
            workflow_id=workflow_id,
            trace_id=trace_id,
            identity=identity,
            use_case=use_case,
            environment=environment,
        )
        return RunRuntime(
            services=self.services,
            run_id=run_id,
            workflow_id=workflow_id,
            trace_id=trace_id,
            identity=identity,
            environment=environment,
            use_case=use_case,
            gateway=gateway,
            budget=budget,
            tracer=tracer,
        )

    def _config(self, run_id: str, runtime: RunRuntime) -> dict[str, Any]:
        return {"configurable": {"thread_id": run_id, "runtime": runtime}}

    async def _invoke(
        self,
        *,
        run_id: str,
        runtime: RunRuntime,
        payload: Any,
        started: float,
        resume: bool,
    ) -> RunResult:
        config = self._config(run_id, runtime)
        failure_message: str | None = None
        failure_status: RunStatus | None = None

        try:
            await asyncio.wait_for(
                self.graph.ainvoke(payload, config),
                timeout=self.settings.run_timeout_seconds,
            )
        except TimeoutError:
            failure_status = RunStatus.TIMEOUT
            failure_message = TIMEOUT_MESSAGE
            metrics.incr("ao.runs.timeout")
            await runtime.tracer.record(TraceAction.RUN_FAILED, TraceStatus.FAILED, error="timeout")
        except BudgetExceededError as exc:
            failure_status = RunStatus.FAILED
            failure_message = BUDGET_MESSAGE
            metrics.incr("ao.runs.budget_exceeded")
            await runtime.tracer.record(TraceAction.RUN_FAILED, TraceStatus.FAILED, error=exc.code)
        except UpstreamUnavailableError as exc:
            failure_status = RunStatus.FAILED
            failure_message = UPSTREAM_MESSAGE
            metrics.incr("ao.runs.upstream_unavailable")
            await runtime.tracer.record(TraceAction.RUN_FAILED, TraceStatus.FAILED, error=exc.code)
        except AppError as exc:
            failure_status = RunStatus.FAILED
            failure_message = FAILED_MESSAGE
            metrics.incr("ao.runs.failed")
            await runtime.tracer.record(TraceAction.RUN_FAILED, TraceStatus.FAILED, error=f"{exc.code}: {exc.message}")
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Unhandled orchestration failure for run %s", run_id)
            failure_status = RunStatus.FAILED
            failure_message = FAILED_MESSAGE
            metrics.incr("ao.runs.failed")
            await runtime.tracer.record(TraceAction.RUN_FAILED, TraceStatus.FAILED, error=str(exc))

        duration_ms = int((time.perf_counter() - started) * 1000)
        return await self._finalize(
            run_id=run_id,
            runtime=runtime,
            config=config,
            duration_ms=duration_ms,
            failure_status=failure_status,
            failure_message=failure_message,
            resumed=resume,
        )

    async def _finalize(
        self,
        *,
        run_id: str,
        runtime: RunRuntime,
        config: dict[str, Any],
        duration_ms: int,
        failure_status: RunStatus | None,
        failure_message: str | None,
        resumed: bool,
    ) -> RunResult:
        values: dict[str, Any] = {}
        paused = False
        try:
            snapshot = await self.graph.aget_state(config)
            values = dict(snapshot.values or {})
            paused = bool(getattr(snapshot, "next", None))
        except Exception as exc:  # pragma: no cover - checkpointer edge cases
            logger.warning("Could not read graph snapshot for run %s: %s", run_id, exc)

        approval: ApprovalRequest | None = None
        if paused:
            approval = await self.services.approvals.get_by_run(run_id)
            status = RunStatus.AWAITING_APPROVAL
            reasons = "; ".join(approval.reasons) if approval and approval.reasons else "consequential action"
            response = APPROVAL_REQUIRED_TEMPLATE.format(
                approval_id=approval.approval_id if approval else "unknown",
                risk=str(approval.risk_level) if approval else "unknown",
                reasons=reasons,
                agents=", ".join(approval.agent_ids) if approval else "unknown",
            )
            metrics.incr("ao.runs.awaiting_approval")
        elif failure_status is not None:
            status = failure_status
            response = failure_message or FAILED_MESSAGE
            metrics.incr(f"ao.runs.{status}")
        else:
            status = _to_status(values.get("status"), RunStatus.FAILED)
            response = values.get("response") or FAILED_MESSAGE
            metrics.incr(f"ao.runs.{status}")

        plan = Plan.model_validate(values["plan"]) if values.get("plan") else None
        agent_results = [InvocationResult.model_validate(item) for item in values.get("results", [])]
        selected = [agent.agent_id for agent in plan.selected_agents] if plan else []
        usage = runtime.budget.snapshot()
        langfuse_url = runtime.tracer.langfuse_url
        error = values.get("error")
        if failure_status is not None:
            error = error or str(failure_status)

        # Persist the outcome and the policy decisions that led to it.
        async with self.database.session() as session:
            await RunRepository(session).save_state(
                run_id,
                status=status,
                plan=plan,
                selected_agents=selected,
                response=response,
                error=error,
                approval_id=approval.approval_id if approval else None,
                usage=usage,
                langfuse_url=langfuse_url,
                completed=status
                in {
                    RunStatus.COMPLETED,
                    RunStatus.FAILED,
                    RunStatus.TIMEOUT,
                    RunStatus.REJECTED,
                    RunStatus.DENIED,
                    RunStatus.NO_MATCH,
                    RunStatus.INVALID,
                },
            )
            decisions = values.get("policy_decisions") or []
            if decisions:
                await PolicyDecisionRepository(session).record_many(
                    run_id, [PolicyDecision.model_validate(item) for item in decisions]
                )

        record = RunRecord(
            run_id=run_id,
            workflow_id=runtime.workflow_id,
            trace_id=runtime.trace_id,
            status=status,
            query=values.get("query", ""),
            environment=runtime.environment,
            use_case=runtime.use_case,
            identity=runtime.identity,
            plan=plan,
            selected_agents=selected,
            response=response,
            error=error,
            approval_id=approval.approval_id if approval else None,
            usage=usage,
            langfuse_url=langfuse_url,
        )
        return RunResult(
            run_id=record.run_id,
            workflow_id=record.workflow_id,
            trace_id=record.trace_id,
            status=status,
            response=response,
            plan=plan,
            agent_results=agent_results,
            approval=approval,
            policy_decisions=[PolicyDecision.model_validate(item) for item in values.get("policy_decisions", [])],
            usage=usage,
            langfuse_url=langfuse_url,
            duration_ms=duration_ms,
        )
