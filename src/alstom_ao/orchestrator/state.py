"""Graph state: plain, checkpoint-serializable data only.

Card **snapshots** are pinned into the state at planning time. That matters for
approvals: the version a human approved is exactly the version that executes,
even if the registry changes while the run is paused.
"""

from __future__ import annotations

from typing import Any, TypedDict


class OrchestratorState(TypedDict, total=False):
    run_id: str
    workflow_id: str
    trace_id: str
    query: str
    environment: str
    use_case: str
    conversation: list[dict[str, str]]

    identity: dict[str, Any]

    # Inbound A2A delegation depth (0 for a UI-originated request). Guards
    # runaway agent-to-agent chains.
    hop_depth: int

    catalog: list[dict[str, Any]]
    plan: dict[str, Any]
    agent_snapshots: list[dict[str, Any]]

    policy_decisions: list[dict[str, Any]]
    approval_id: str | None
    approval_status: str | None

    results: list[dict[str, Any]]
    failures: list[dict[str, Any]]
    executed_agents: list[str]

    response: str
    status: str
    error: str | None
    usage: dict[str, Any]
    duration_ms: int


def selected_ids(state: OrchestratorState) -> list[str]:
    plan = state.get("plan") or {}
    return [item["agent_id"] for item in plan.get("selected_agents", [])]


def hops_remaining(state: OrchestratorState) -> int:
    """Agent invocations still allowed, derived from the persisted budget."""

    usage = state.get("usage") or {}
    limits = usage.get("limits") or {}
    max_hops = int(limits.get("max_hops") or 0)
    used = int(usage.get("hops") or 0)
    if max_hops <= 0:
        return 10**6
    return max(0, max_hops - used)
