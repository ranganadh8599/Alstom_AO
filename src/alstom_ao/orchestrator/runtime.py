"""Per-run runtime objects injected through LangGraph's ``config``.

LangGraph passes ``RunnableConfig`` to nodes as the second argument. We put the
run-scoped runtime there (gateway, budget, tracer) and the long-lived services in
the node closures. Nothing runtime-specific is checkpointed, so a resumed run
gets a fresh gateway/tracer built from the persisted run record.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from alstom_ao.adapters.factory import AdapterRegistry
from alstom_ao.approvals.service import ApprovalService
from alstom_ao.config import Settings
from alstom_ao.db.session import Database
from alstom_ao.domain.models import IdentityContext
from alstom_ao.llm.gateway import LLMBudget, ModelGateway
from alstom_ao.observability.tracing import Tracer, TracingService
from alstom_ao.orchestrator.planner import Planner
from alstom_ao.policy.service import PolicyService
from alstom_ao.registry.service import AgentRegistryService


@dataclass
class NodeServices:
    """Long-lived, run-independent collaborators."""

    settings: Settings
    database: Database
    registry: AgentRegistryService
    policy: PolicyService
    approvals: ApprovalService
    adapters: AdapterRegistry
    planner: Planner
    tracing: TracingService


@dataclass
class RunRuntime:
    """Run-scoped collaborators, rebuilt on every invoke/resume."""

    services: NodeServices
    run_id: str
    workflow_id: str
    trace_id: str
    identity: IdentityContext
    environment: str
    use_case: str
    gateway: ModelGateway
    budget: LLMBudget
    tracer: Tracer


def runtime_from(config: dict[str, Any] | None) -> RunRuntime:
    configurable = (config or {}).get("configurable") or {}
    runtime = configurable.get("runtime")
    if not isinstance(runtime, RunRuntime):
        raise RuntimeError("Run runtime is missing from the graph configuration.")
    return runtime
