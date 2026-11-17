"""LangGraph wiring for the orchestration layer.

    START -> validate -> plan -> authorize -> approval_gate -> execute -> synthesize -> finalize -> END

Each conditional edge is a terminal branch with a user-facing outcome:
invalid input, no matching agent, policy denial, or human rejection. That is what
keeps the layer from ever returning an empty or fabricated answer.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from alstom_ao.config import Settings
from alstom_ao.domain.enums import RunStatus
from alstom_ao.orchestrator.nodes import (
    approval_gate_node,
    authorize_node,
    execute_node,
    finalize_node,
    plan_node,
    synthesize_node,
    validate_node,
)
from alstom_ao.orchestrator.runtime import NodeServices
from alstom_ao.orchestrator.state import OrchestratorState

logger = logging.getLogger(__name__)

NODE_NAMES = ["validate", "plan", "authorize", "approval", "execute", "synthesize", "finalize"]


def _route_after_validate(state: OrchestratorState) -> str:
    return END if state.get("status") == str(RunStatus.INVALID) else "plan"


def _route_after_plan(state: OrchestratorState) -> str:
    plan = state.get("plan") or {}
    if not plan.get("selected_agents"):
        return END
    return "authorize"


def _route_after_authorize(state: OrchestratorState) -> str:
    return END if state.get("status") == str(RunStatus.DENIED) else "approval"


def _route_after_approval(state: OrchestratorState) -> str:
    if state.get("status") in {str(RunStatus.REJECTED), str(RunStatus.DENIED)}:
        return END
    if state.get("approval_status") == "rejected":
        return END
    return "execute"


def build_graph(services: NodeServices, checkpointer: Any):
    graph = StateGraph(OrchestratorState)

    graph.add_node("validate", validate_node)
    graph.add_node("plan", plan_node)
    graph.add_node("authorize", authorize_node)
    graph.add_node("approval", approval_gate_node)
    graph.add_node("execute", execute_node)
    graph.add_node("synthesize", synthesize_node)
    graph.add_node("finalize", finalize_node)

    graph.add_edge(START, "validate")
    graph.add_conditional_edges("validate", _route_after_validate, {"plan": "plan", END: END})
    graph.add_conditional_edges("plan", _route_after_plan, {"authorize": "authorize", END: END})
    graph.add_conditional_edges("authorize", _route_after_authorize, {"approval": "approval", END: END})
    graph.add_conditional_edges("approval", _route_after_approval, {"execute": "execute", END: END})
    graph.add_edge("execute", "synthesize")
    graph.add_edge("synthesize", "finalize")
    graph.add_edge("finalize", END)

    return graph.compile(checkpointer=checkpointer)


@asynccontextmanager
async def checkpointer_for(settings: Settings) -> AsyncIterator[Any]:
    """Postgres-backed checkpoints when available, in-memory otherwise.

    In-memory checkpoints are fine for single-process dev but do not survive a
    restart. The run/trace/approval tables always do, so the audit trail is
    intact either way; only an in-flight *pause* needs the durable checkpointer.
    """

    if settings.database_url.startswith("postgresql"):
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            conn_string = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
            async with AsyncPostgresSaver.from_conn_string(conn_string) as saver:
                await saver.setup()
                yield saver
                return
        except Exception as exc:
            logger.warning(
                "Durable Postgres checkpointer unavailable (%s); falling back to MemorySaver. "
                "Install 'langgraph-checkpoint-postgres' and 'psycopg[binary]' for restart-safe pauses.",
                exc,
            )
    yield MemorySaver()
