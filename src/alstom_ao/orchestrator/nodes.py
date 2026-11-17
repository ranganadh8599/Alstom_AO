"""Graph nodes.

Flow: validate -> plan -> authorize -> approval_gate -> execute -> synthesize -> finalize

Every node records its own trace events; every agent invocation is wrapped in a
Langfuse ``agent`` span carrying the full common trace context.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from alstom_ao.approvals.service import assess_approval
from alstom_ao.config import Settings
from alstom_ao.domain.enums import (
    ApprovalStatus,
    RunStatus,
    TraceAction,
    TraceStatus,
)
from alstom_ao.domain.models import (
    AgentCard,
    IdentityContext,
    InvocationRequest,
    InvocationResult,
    Plan,
    PolicyDecision,
    TraceContext,
)
from alstom_ao.errors import AppError, BudgetExceededError
from alstom_ao.llm.gateway import BudgetDecision, BudgetState
from alstom_ao.llm.grounding import apply_grounding
from alstom_ao.llm.prompts import SYNTHESIZER_SYSTEM, build_synthesizer_user_prompt
from alstom_ao.observability.alerts import emit_alert
from alstom_ao.observability.metrics import metrics
from alstom_ao.orchestrator.runtime import RunRuntime, runtime_from
from alstom_ao.orchestrator.state import OrchestratorState, hops_remaining

logger = logging.getLogger(__name__)

EMPTY_QUERY_MESSAGE = (
    "Please describe what you need help with. For example: "
    "\"Check invoice INV-2024-0912 for duplicate charges\" or "
    "\"How many days of annual leave do I get?\""
)
NO_MATCH_TEMPLATE = (
    "I couldn't find a registered agent that can answer this request. "
    "This environment currently covers: {catalog}. "
    "Try rephrasing the request around one of those capabilities, or ask the platform team to "
    "register an agent for this use case."
)
DENIED_TEMPLATE = (
    "Access denied. Your request matched {count} agent(s), but policy does not permit you to call them: {reasons}. "
    "If you believe this is wrong, ask your platform administrator to review your roles."
)
REJECTED_TEMPLATE = (
    "This action was not approved by a human reviewer{reason}. Nothing was executed. "
    "You can adjust the request or ask an approver to review it."
)
ALL_FAILED_TEMPLATE = (
    "I couldn't complete the request. {count} agent(s) were selected but none returned a usable answer{detail}. "
    "Please try again later or contact the platform team if this persists."
)
BUDGET_MESSAGE = (
    "This request exceeded the orchestration budget (model calls, tokens or agent hops). "
    "No further agents were called. Please narrow the request or ask the platform team to raise the limit."
)
HOP_LIMIT_TEMPLATE = (
    "This request stopped because the agent delegation limit was reached "
    "(depth {depth}, limit {limit}). No further agents were called. "
    "If this came from a legitimate workflow, ask the platform team to raise the hop limit."
)
BUDGET_PARTIAL_NOTE = (
    "\n\n> Budget note: this answer was assembled from completed agent results without the final "
    "model synthesis, because the run reached its {dimension} limit. Results are complete; wording "
    "is not polished."
)


def _settings(runtime: RunRuntime) -> Settings:
    return runtime.services.settings


def _identity(state: OrchestratorState) -> IdentityContext:
    return IdentityContext.model_validate(state["identity"])


async def _apply_soft_tier(
    runtime: RunRuntime,
    decision: BudgetDecision,
    identity: IdentityContext,
    state: OrchestratorState,
) -> None:
    """Soft crossing: record, alert once, and downgrade the model if configured.

    Never raises and never blocks: this is the tier that keeps long A2A work
    alive instead of cascading a failure through the graph.
    """

    settings = runtime.services.settings
    first = runtime.budget.claim_warning(decision)
    if not first:
        return

    downgraded = False
    if settings.budget_degrade_model:
        downgraded = runtime.gateway.downgrade(settings.budget_degrade_model)

    await runtime.tracer.record(
        TraceAction.BUDGET_DEGRADED if downgraded else TraceAction.BUDGET_WARNING,
        TraceStatus.SUCCEEDED,
        output={
            "detail": decision.describe(),
            "dimension": decision.dimension,
            "percent": round(decision.percent, 4),
            "model": runtime.gateway.current_model(),
            "downgraded": downgraded,
        },
        metadata={"usage": runtime.budget.snapshot()},
    )

    emit_alert(
        settings,
        decision=decision,
        run_id=runtime.run_id,
        workflow_id=runtime.workflow_id,
        user_id=identity.user.user_id,
        environment=runtime.environment,
        model=runtime.gateway.current_model(),
    )
    metrics.incr("ao.budget.warning")


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


async def validate_node(state: OrchestratorState, config: RunnableConfig) -> dict[str, Any]:
    runtime = runtime_from(config)
    query = (state.get("query") or "").strip()
    await runtime.tracer.record(
        TraceAction.INPUT_VALIDATED,
        TraceStatus.SUCCEEDED,
        input={"query_length": len(query), "environment": state.get("environment")},
    )

    if not query:
        return {"status": str(RunStatus.INVALID), "response": EMPTY_QUERY_MESSAGE, "error": "empty_query"}
    max_chars = _settings(runtime).max_query_chars
    if len(query) > max_chars:
        return {
            "status": str(RunStatus.INVALID),
            "response": f"Your request is too long ({len(query)} characters). Please shorten it to {max_chars} characters or fewer.",
            "error": "query_too_long",
        }
    return {"query": query, "status": str(RunStatus.PLANNING)}


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


async def plan_node(state: OrchestratorState, config: RunnableConfig) -> dict[str, Any]:
    runtime = runtime_from(config)
    identity = _identity(state)
    settings = _settings(runtime)
    query = state["query"]

    async with runtime.tracer.span(
        "orchestrator.plan",
        input={"query": query[:1000]},
        metadata={"roles": identity.user.roles},
    ) as observation:
        catalog = await runtime.services.registry.planner_catalog(
            environment=state.get("environment", settings.environment),
            roles=identity.user.roles,
        )
        plan = await runtime.services.planner.plan(
            query=query,
            catalog=catalog,
            roles=identity.user.roles,
            environment=state.get("environment", settings.environment),
            use_case=state.get("use_case", "general"),
            gateway=runtime.gateway,
            conversation=state.get("conversation"),
        )
        observation.update(
            output={
                "selected_agents": [agent.agent_id for agent in plan.selected_agents],
                "confidence": plan.confidence,
                "source": plan.source,
            }
        )

    # Pin the exact card versions that were planned. Approvals then bind to the
    # version a human actually approved.
    snapshots: list[dict[str, Any]] = []
    for planned in plan.selected_agents:
        try:
            card = await runtime.services.registry.get(planned.agent_id)
        except AppError:
            continue
        snapshots.append(card.model_dump(mode="json"))

    await runtime.tracer.record(
        TraceAction.PLAN_CREATED if plan.selected_agents else TraceAction.NO_MATCH,
        TraceStatus.SUCCEEDED if plan.selected_agents else TraceStatus.SKIPPED,
        input={"query": query[:1000], "catalog_size": len(catalog)},
        output={
            "plan": plan.model_dump(mode="json"),
            "selected_agents": [agent.agent_id for agent in plan.selected_agents],
            "confidence": plan.confidence,
            "source": plan.source,
        },
    )

    if not plan.selected_agents:
        summary = runtime.services.registry.catalog_summary(catalog)
        return {
            "plan": plan.model_dump(mode="json"),
            "catalog": catalog,
            "agent_snapshots": [],
            "status": str(RunStatus.NO_MATCH),
            "response": NO_MATCH_TEMPLATE.format(catalog=summary),
        }

    return {
        "plan": plan.model_dump(mode="json"),
        "catalog": catalog,
        "agent_snapshots": snapshots,
        "status": str(RunStatus.EXECUTING),
        "usage": runtime.budget.snapshot(),
    }


# ---------------------------------------------------------------------------
# authorize
# ---------------------------------------------------------------------------


async def authorize_node(state: OrchestratorState, config: RunnableConfig) -> dict[str, Any]:
    runtime = runtime_from(config)
    identity = _identity(state)
    settings = _settings(runtime)
    plan = Plan.model_validate(state["plan"])
    cards = {item["agent_id"]: AgentCard.model_validate(item) for item in state.get("agent_snapshots", [])}

    decisions: list[PolicyDecision] = []
    allowed = []
    denied_reasons: list[str] = []

    for planned in plan.selected_agents:
        card = cards.get(planned.agent_id)
        if not card:
            continue
        decision = await runtime.services.policy.evaluate_agent(
            identity=identity,
            agent=card,
            action=planned.action,
            environment=state.get("environment", settings.environment),
            use_case=state.get("use_case", "general"),
            query=state["query"],
        )
        decisions.append(decision)
        await runtime.tracer.record(
            TraceAction.POLICY_EVALUATED,
            TraceStatus.SUCCEEDED if decision.allowed else TraceStatus.DENIED,
            agent_id=card.agent_id,
            agent_version=card.version,
            protocol=str(card.protocol),
            input={"action": str(planned.action)},
            output={
                "allowed": decision.allowed,
                "reasons": decision.reasons,
                "source": decision.source,
                "requires_approval": decision.requires_approval,
            },
        )
        if decision.allowed:
            allowed.append(planned)
        else:
            denied_reasons.extend(f"{card.agent_id}: {reason}" for reason in decision.reasons)

    if not allowed:
        return {
            "policy_decisions": [decision.model_dump(mode="json") for decision in decisions],
            "status": str(RunStatus.DENIED),
            "response": DENIED_TEMPLATE.format(
                count=len(plan.selected_agents),
                reasons="; ".join(denied_reasons) or "no permitted agent",
            ),
            "error": "policy_denied",
        }

    plan.selected_agents = allowed
    return {
        "plan": plan.model_dump(mode="json"),
        "policy_decisions": [decision.model_dump(mode="json") for decision in decisions],
        "usage": runtime.budget.snapshot(),
    }


# ---------------------------------------------------------------------------
# approval gate (dynamic, interrupt-based)
# ---------------------------------------------------------------------------


async def approval_gate_node(state: OrchestratorState, config: RunnableConfig) -> dict[str, Any]:
    runtime = runtime_from(config)
    identity = _identity(state)
    settings = _settings(runtime)
    plan = Plan.model_validate(state["plan"])
    cards = {item["agent_id"]: AgentCard.model_validate(item) for item in state.get("agent_snapshots", [])}
    decisions = [PolicyDecision.model_validate(item) for item in state.get("policy_decisions", [])]

    assessment = assess_approval(
        plan=plan,
        cards=cards,
        decisions=decisions,
        identity=identity,
        settings=settings,
    )

    if not assessment.required:
        return {"approval_id": None, "approval_status": "not_required"}

    approval, created = await runtime.services.approvals.get_or_create(
        run_id=runtime.run_id,
        assessment=assessment,
        plan=plan,
        query=state["query"],
        identity=identity,
    )
    if created:
        await runtime.tracer.record(
            TraceAction.APPROVAL_REQUESTED,
            TraceStatus.PENDING,
            input={"query": state["query"][:1000], "agents": [agent.agent_id for agent in plan.selected_agents]},
            output={
                "approval_id": approval.approval_id,
                "risk_level": str(assessment.risk_level),
                "reasons": assessment.reasons,
                "expires_at": approval.expires_at.isoformat() if approval.expires_at else None,
            },
            metadata={"initiated_by": identity.user.user_id},
        )

    # Pause the graph. State (including the pinned card versions) is persisted by
    # the checkpointer; the process may die here and resume hours later.
    resume_value = interrupt(
        {
            "type": "approval_required",
            "approval_id": approval.approval_id,
            "run_id": runtime.run_id,
            "risk_level": str(assessment.risk_level),
            "reasons": assessment.reasons,
            "agent_ids": [agent.agent_id for agent in plan.selected_agents],
            "expires_at": approval.expires_at.isoformat() if approval.expires_at else None,
        }
    )

    if isinstance(resume_value, dict) and "approved" in resume_value:
        approved = bool(resume_value.get("approved"))
        decided_by = str(resume_value.get("decided_by") or "unknown")
        note = resume_value.get("note")
        await runtime.services.approvals.record_decision_in_graph(
            approval.approval_id, approved=approved, decided_by=decided_by, note=note
        )
    else:
        # Resumed without a payload (e.g. decision taken out of band): trust the DB.
        stored = await runtime.services.approvals.get(approval.approval_id)
        approved = stored.status == ApprovalStatus.APPROVED
        decided_by = stored.decided_by or "unknown"
        note = stored.decision_note

    await runtime.tracer.record(
        TraceAction.APPROVAL_DECIDED,
        TraceStatus.SUCCEEDED if approved else TraceStatus.DENIED,
        output={"approval_id": approval.approval_id, "approved": approved, "decided_by": decided_by},
    )

    if not approved:
        reason = f" ({note})" if note else ""
        return {
            "approval_id": approval.approval_id,
            "approval_status": str(ApprovalStatus.REJECTED),
            "status": str(RunStatus.REJECTED),
            "response": REJECTED_TEMPLATE.format(reason=reason),
            "error": "approval_rejected",
        }

    return {
        "approval_id": approval.approval_id,
        "approval_status": str(ApprovalStatus.APPROVED),
        "status": str(RunStatus.EXECUTING),
        "usage": runtime.budget.snapshot(),
    }


# ---------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------


async def execute_node(state: OrchestratorState, config: RunnableConfig) -> dict[str, Any]:
    runtime = runtime_from(config)
    settings = _settings(runtime)
    identity = _identity(state)
    plan = Plan.model_validate(state["plan"])
    cards = {item["agent_id"]: AgentCard.model_validate(item) for item in state.get("agent_snapshots", [])}
    semaphore = asyncio.Semaphore(max(1, settings.max_parallel_agents))
    ordered = sorted(plan.selected_agents, key=lambda agent: agent.priority)
    order_index = {agent.agent_id: index for index, agent in enumerate(ordered)}

    # --- delegation-depth guard -------------------------------------------
    # An inbound A2A call that is already N deep must not fan out again; that is
    # how agent-to-agent loops burn budget before dollars ever register.
    incoming_depth = int(state.get("hop_depth") or 0)
    if settings.max_a2a_depth > 0 and incoming_depth >= settings.max_a2a_depth:
        await runtime.tracer.record(
            TraceAction.HOP_LIMIT_HIT,
            TraceStatus.DENIED,
            output={"hop_depth": incoming_depth, "max_a2a_depth": settings.max_a2a_depth},
        )
        return {
            "status": str(RunStatus.FAILED),
            "response": HOP_LIMIT_TEMPLATE.format(depth=incoming_depth, limit=settings.max_a2a_depth),
            "error": "hop_limit_exceeded",
            "usage": runtime.budget.snapshot(),
        }

    # --- per-run hop cap ---------------------------------------------------
    remaining_hops = hops_remaining(state)
    if remaining_hops <= 0:
        await runtime.tracer.record(
            TraceAction.HOP_LIMIT_HIT,
            TraceStatus.DENIED,
            output={"reason": "max_agent_hops", "max_agent_hops": settings.max_agent_hops},
        )
        return {
            "status": str(RunStatus.FAILED),
            "response": HOP_LIMIT_TEMPLATE.format(depth=0, limit=settings.max_agent_hops),
            "error": "hop_limit_exceeded",
            "usage": runtime.budget.snapshot(),
        }
    if len(ordered) > remaining_hops:
        # Truncate the plan, not the budget: the highest-priority agents run.
        dropped = [agent.agent_id for agent in ordered[remaining_hops:]]
        ordered = ordered[:remaining_hops]
        plan.selected_agents = ordered
        order_index = {agent.agent_id: index for index, agent in enumerate(ordered)}
        await runtime.tracer.record(
            TraceAction.HOP_LIMIT_HIT,
            TraceStatus.SKIPPED,
            output={"max_agent_hops": settings.max_agent_hops, "skipped_agents": dropped},
        )

    # --- budget tier: warn/degrade or block new work ----------------------
    decision = runtime.budget.evaluate()
    if decision.state is BudgetState.EXCEEDED:
        # Root/work level: starting fresh agent work here would only deepen the
        # overflow. Nothing has been invoked yet, so fail cleanly with a budget
        # message instead of a half-executed graph.
        await runtime.tracer.record(
            TraceAction.BUDGET_EXCEEDED,
            TraceStatus.DENIED,
            output={"detail": decision.describe(), "before": "agent_execution"},
            metadata={"usage": runtime.budget.snapshot()},
        )
        return {
            "status": str(RunStatus.FAILED),
            "response": BUDGET_MESSAGE,
            "error": "budget_exceeded",
            "usage": runtime.budget.snapshot(),
        }
    if decision.state is BudgetState.WARN:
        await _apply_soft_tier(runtime, decision, identity, state)

    async def run_one(planned) -> tuple[InvocationResult | None, dict[str, Any] | None]:
        card = cards.get(planned.agent_id)
        if card is None:
            return None, {"agent_id": planned.agent_id, "reason": "card_snapshot_missing"}

        trace = TraceContext(
            workflow_id=runtime.workflow_id,
            trace_id=runtime.trace_id,
            run_id=runtime.run_id,
            agent_id=card.agent_id,
            agent_version=card.version,
            agent_name=card.name,
            protocol=str(card.protocol),
            action=planned.action,
            initiated_by=identity.user.user_id,
            authenticated_service=identity.service.service_id,
            use_case=state.get("use_case", "general"),
            environment=state.get("environment", settings.environment),
            previous_agents=[agent.agent_id for agent in ordered if order_index[agent.agent_id] < order_index[planned.agent_id]],
            next_agents=[agent.agent_id for agent in ordered if order_index[agent.agent_id] > order_index[planned.agent_id]],
        )
        request = InvocationRequest(
            query=state["query"],
            trace=trace,
            identity=identity,
            instructions=planned.instructions or None,
            context={"use_case": state.get("use_case", "general")},
        )
        await runtime.tracer.record(
            TraceAction.AGENT_INVOKED,
            TraceStatus.STARTED,
            agent_id=card.agent_id,
            agent_version=card.version,
            protocol=str(card.protocol),
            input={
                "query": state["query"][:1000],
                "instructions": planned.instructions[:500] or None,
                "previous_agents": trace.previous_agents,
                "next_agents": trace.next_agents,
                "action": str(planned.action),
            },
            metadata={"endpoint": card.endpoint, "owner": card.owner, "environment": trace.environment},
        )

        async with semaphore:
            runtime.budget.record_hop()
            async with runtime.tracer.span(
                f"agent.{card.agent_id}",
                as_type="agent",
                input={"query": state["query"][:2000], "instructions": planned.instructions[:500] or None},
                agent_id=card.agent_id,
                agent_version=card.version,
                protocol=str(card.protocol),
                action=planned.action,
                metadata={"owner": card.owner, "endpoint": card.endpoint},
            ) as observation:
                started = time.perf_counter()
                try:
                    adapter = runtime.services.adapters.get(card.protocol)
                    result = await adapter.invoke(card, request)
                except AppError as exc:
                    result = InvocationResult(
                        agent_id=card.agent_id,
                        agent_name=card.name,
                        agent_version=card.version,
                        protocol=str(card.protocol),
                        status=TraceStatus.FAILED,
                        error=f"{exc.code}: {exc.message}",
                        latency_ms=int((time.perf_counter() - started) * 1000),
                    )
                except Exception as exc:  # defensive: never fail the whole run for one agent
                    logger.exception("Unexpected agent failure for %s", card.agent_id)
                    result = InvocationResult(
                        agent_id=card.agent_id,
                        agent_name=card.name,
                        agent_version=card.version,
                        protocol=str(card.protocol),
                        status=TraceStatus.FAILED,
                        error=f"unexpected_error: {exc}",
                        latency_ms=int((time.perf_counter() - started) * 1000),
                    )
                observation.update(
                    output={"status": str(result.status), "text": result.text[:2000], "error": result.error},
                    metadata={"latency_ms": result.latency_ms, "retry_count": result.retry_count},
                )

        if result.succeeded:
            await runtime.tracer.record(
                TraceAction.AGENT_COMPLETED,
                TraceStatus.SUCCEEDED,
                agent_id=card.agent_id,
                agent_version=card.version,
                protocol=str(card.protocol),
                input={"action": str(planned.action)},
                output={"text": result.text[:4000], "artifacts": result.artifacts},
                latency_ms=result.latency_ms,
                retry_count=result.retry_count,
            )
            return result, None

        await runtime.tracer.record(
            TraceAction.AGENT_FAILED,
            TraceStatus.FAILED,
            agent_id=card.agent_id,
            agent_version=card.version,
            protocol=str(card.protocol),
            input={"action": str(planned.action)},
            output={"error": result.error},
            latency_ms=result.latency_ms,
            retry_count=result.retry_count,
            error=result.error,
        )
        return None, {"agent_id": card.agent_id, "agent_name": card.name, "error": result.error}

    outcomes = await asyncio.gather(*(run_one(planned) for planned in ordered))
    results = [result.model_dump(mode="json") for result, _ in outcomes if result is not None]
    failures = [failure for _, failure in outcomes if failure is not None]

    await runtime.tracer.record(
        "agents.fan_in",
        TraceStatus.SUCCEEDED if results else TraceStatus.FAILED,
        output={"succeeded": [item["agent_id"] for item in results], "failed": [item["agent_id"] for item in failures]},
        metadata={"phase": "fan_in"},
    )

    return {
        "results": results,
        "failures": failures,
        "executed_agents": [item["agent_id"] for item in results],
        # Reflect hop-cap truncation in the persisted plan.
        "plan": plan.model_dump(mode="json"),
        "usage": runtime.budget.snapshot(),
    }


# ---------------------------------------------------------------------------
# synthesize
# ---------------------------------------------------------------------------


def _compose_fallback(results: list[InvocationResult]) -> str:
    if len(results) == 1:
        return results[0].text
    sections = [f"### {result.agent_name}\n{result.text}" for result in results if result.text]
    return "\n\n".join(sections)


def _agent_text(result: InvocationResult) -> str:
    """Everything an agent reported, used as the grounding source of truth."""

    parts = [result.text or ""]
    for artifact in result.artifacts or []:
        for part in artifact.get("parts") or []:
            text = part.get("text")
            if text:
                parts.append(str(text))
    parts.append(json.dumps(result.structured or {}, default=str))
    return "\n".join(parts)


async def synthesize_node(state: OrchestratorState, config: RunnableConfig) -> dict[str, Any]:
    runtime = runtime_from(config)
    results = [InvocationResult.model_validate(item) for item in state.get("results", [])]
    failures = state.get("failures", [])

    if not results:
        detail = ""
        if failures:
            detail = f" ({len(failures)} failure(s) recorded)"
        return {
            "status": str(RunStatus.FAILED),
            "response": ALL_FAILED_TEMPLATE.format(count=len(state.get("plan", {}).get("selected_agents", [])), detail=detail),
            "error": "all_agents_failed",
            "usage": runtime.budget.snapshot(),
        }

    budget_note = ""
    grounding_note: str | None = None
    async with runtime.tracer.span("orchestrator.synthesize", input={"result_count": len(results)}) as observation:
        if runtime.gateway.is_mock:
            response = _compose_fallback(results)
        else:
            decision = runtime.budget.evaluate()
            if decision.state is BudgetState.EXCEEDED:
                # Agent work is already paid for and complete. Do not discard it
                # just because the synthesis call cannot be afforded - that is the
                # cascade this design exists to prevent.
                response = _compose_fallback(results)
                budget_note = BUDGET_PARTIAL_NOTE.format(dimension=decision.dimension or "budget")
                await runtime.tracer.record(
                    TraceAction.BUDGET_EXCEEDED,
                    TraceStatus.SKIPPED,
                    output={"detail": decision.describe(), "skipped": "llm_synthesis"},
                    metadata={"usage": runtime.budget.snapshot()},
                )
                metrics.incr("ao.budget.synthesis_skipped")
            else:
                if decision.state is BudgetState.WARN:
                    await _apply_soft_tier(runtime, decision, _identity(state), state)
                try:
                    identity = _identity(state)
                    llm_result = await runtime.gateway.complete_text(
                        system=SYNTHESIZER_SYSTEM,
                        user=build_synthesizer_user_prompt(
                            query=state["query"],
                            results=[result.model_dump(mode="json") for result in results],
                            failures=failures,
                            identity_label=f"{identity.user.user_id} ({', '.join(identity.user.roles)})",
                        ),
                    )
                    response = llm_result.text.strip() or _compose_fallback(results)
                except BudgetExceededError:
                    # Crossed the cap between the check and the call: degrade, do not fail.
                    logger.warning("Synthesis skipped by budget limit")
                    response = _compose_fallback(results)
                    budget_note = BUDGET_PARTIAL_NOTE.format(dimension="budget")
                    metrics.incr("ao.budget.synthesis_skipped")
                except Exception as exc:
                    logger.warning("Synthesis failed, falling back to concatenation: %s", exc)
                    response = _compose_fallback(results)

            # A prompt rule cannot guarantee the model kept its arithmetic out, so
            # every generated answer is checked against the numbers the agents
            # actually reported. "off" skips the check entirely.
            grounding_mode = runtime.services.settings.synthesis_grounding
            if not runtime.gateway.is_mock and grounding_mode != "off":
                response, report, grounding_note = apply_grounding(
                    answer=response,
                    results=[_agent_text(result) for result in results],
                    query=state.get("query", ""),
                    mode=grounding_mode,
                    fallback=_compose_fallback(results),
                )
                if not report.ok:
                    metrics.incr("ao.synthesis.ungrounded")
                    await runtime.tracer.record(
                        TraceAction.SYNTHESIS_UNGROUNDED,
                        TraceStatus.SUCCEEDED if grounding_note == "warned" else TraceStatus.FAILED,
                        output={
                            "ungrounded": sorted(report.ungrounded),
                            "grounded": sorted(report.grounded),
                            "mode": grounding_mode,
                            "action": grounding_note,
                        },
                    )

        if failures:
            response += (
                f"\n\n> Note: {len(failures)} agent(s) could not complete: "
                + ", ".join(failure.get("agent_name") or failure.get("agent_id", "unknown") for failure in failures)
                + "."
            )
        response += budget_note
        observation.update(
            output={
                "response": response[:2000],
                "failures": len(failures),
                "budget_limited": bool(budget_note),
                "grounding": grounding_note or "ok",
            }
        )

    await runtime.tracer.record(
        TraceAction.SYNTHESIS_COMPLETED,
        TraceStatus.SUCCEEDED,
        output={
            "response": response[:4000],
            "failed_agents": [failure.get("agent_id") for failure in failures],
            "grounding": grounding_note or "ok",
        },
        metadata={"usage": runtime.budget.snapshot()},
    )
    return {"response": response, "status": str(RunStatus.COMPLETED), "usage": runtime.budget.snapshot()}


# ---------------------------------------------------------------------------
# finalize
# ---------------------------------------------------------------------------


async def finalize_node(state: OrchestratorState, config: RunnableConfig) -> dict[str, Any]:
    runtime = runtime_from(config)
    status = state.get("status", str(RunStatus.COMPLETED))
    succeeded = status == str(RunStatus.COMPLETED)
    await runtime.tracer.record(
        TraceAction.RUN_COMPLETED if succeeded else TraceAction.RUN_FAILED,
        TraceStatus.SUCCEEDED if succeeded else TraceStatus.FAILED,
        output={
            "status": status,
            "response_length": len(state.get("response", "")),
            "agents": state.get("executed_agents", []),
            "failures": [failure.get("agent_id") for failure in state.get("failures", [])],
        },
        metadata={"usage": runtime.budget.snapshot()},
    )
    return {"usage": runtime.budget.snapshot(), "executed_agents": state.get("executed_agents", [])}
