"""Dynamic human-in-the-loop approval flow."""

from __future__ import annotations

from typing import Any

import pytest

from tests.conftest import agent_payload

FINANCE_HIGH_RISK = dict(
    agent_id="finance-invoice-auditor",
    endpoint="http://test/agents/finance",
    capabilities=["invoice audit", "duplicate invoice detection", "invoice adjustment"],
    tags=["finance", "invoice"],
    examples=["Create an adjustment task for invoice INV-2024-0912"],
    allowed_roles=["admin"],
    allowed_actions=["read", "query", "analyze", "write"],
    requires_approval_for=["write", "update", "payment"],
    risk_level="high",
)

HR_READ_ONLY = dict(
    agent_id="hr-policy-advisor",
    endpoint="http://test/agents/hr",
    capabilities=["hr policy lookup", "leave and absence rules"],
    tags=["hr", "policy", "leave"],
    examples=["How many days of annual leave do I get?"],
    allowed_roles=["admin"],
    allowed_actions=["read", "query", "analyze"],
    risk_level="low",
)

WRITE_QUERY = "Create an adjustment task for invoice INV-2024-0912 and update the vendor balance"


async def register(client, headers, **overrides) -> dict[str, Any]:
    response = await client.post("/v1/agents", json=agent_payload(**overrides), headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["agent"]


@pytest.fixture
async def finance(client, admin_headers):
    return await register(client, admin_headers, **FINANCE_HIGH_RISK)


@pytest.fixture
async def hr(client, admin_headers):
    return await register(client, admin_headers, **HR_READ_ONLY)


async def test_write_action_pauses_for_approval(client, user_headers, finance, a2a):
    response = await client.post("/v1/orchestrate", json={"query": WRITE_QUERY}, headers=user_headers)
    run = response.json()["run"]

    assert run["status"] == "awaiting_approval"
    assert run["approval"] is not None
    approval = run["approval"]
    assert approval["risk_level"] == "high"
    assert approval["status"] == "pending"
    assert any(
        token in " ".join(approval["reasons"]) for token in ("write", "update", "payment", "high risk")
    )
    assert "needs human approval" in run["response"]
    assert a2a.calls == []  # nothing executed before approval

    pending = await client.get("/v1/approvals?status=pending", headers=user_headers)
    assert pending.status_code == 200
    assert [item["approval_id"] for item in pending.json()["items"]] == [approval["approval_id"]]

    stored = await client.get(f"/v1/runs/{run['run_id']}", headers=user_headers)
    assert stored.json()["status"] == "awaiting_approval"


async def test_approver_can_approve_and_run_executes(client, user_headers, approver_headers, finance, a2a):
    started = await client.post("/v1/orchestrate", json={"query": WRITE_QUERY}, headers=user_headers)
    approval_id = started.json()["run"]["approval"]["approval_id"]

    decision = await client.post(
        f"/v1/approvals/{approval_id}/decision",
        json={"approved": True, "note": "checked with the vendor"},
        headers=approver_headers,
    )
    assert decision.status_code == 200, decision.text
    body = decision.json()
    assert body["approval"]["status"] == "approved"
    assert body["approval"]["decided_by"] == "approver-bob"
    assert body["run"]["status"] == "completed"
    assert "finance handled" in body["run"]["response"]
    assert len(a2a.calls_for("/agents/finance")) == 1

    trace = await client.get(f"/v1/runs/{body['run']['run_id']}/trace", headers=user_headers)
    actions = [event["action"] for event in trace.json()["events"]]
    assert "approval.requested" in actions
    assert "approval.decided" in actions
    assert "agent.completed" in actions


async def test_rejection_blocks_execution(client, user_headers, approver_headers, finance, a2a):
    started = await client.post("/v1/orchestrate", json={"query": WRITE_QUERY}, headers=user_headers)
    approval_id = started.json()["run"]["approval"]["approval_id"]

    decision = await client.post(
        f"/v1/approvals/{approval_id}/decision",
        json={"approved": False, "note": "not budgeted"},
        headers=approver_headers,
    )
    assert decision.status_code == 200, decision.text
    body = decision.json()
    assert body["approval"]["status"] == "rejected"
    assert body["run"]["status"] == "rejected"
    assert "not approved" in body["run"]["response"]
    assert a2a.calls == []


async def test_self_approval_is_forbidden(client, user_headers, finance, a2a):
    started = await client.post("/v1/orchestrate", json={"query": WRITE_QUERY}, headers=user_headers)
    approval_id = started.json()["run"]["approval"]["approval_id"]

    decision = await client.post(
        f"/v1/approvals/{approval_id}/decision",
        json={"approved": True},
        headers=user_headers,  # alice requested it; alice has no approver role
    )
    assert decision.status_code == 403
    assert decision.json()["error"]["code"] == "forbidden"
    assert a2a.calls == []


async def test_read_only_question_never_pauses(client, user_headers, hr, a2a):
    response = await client.post(
        "/v1/orchestrate",
        json={"query": "How many days of annual leave do I get?"},
        headers=user_headers,
    )
    run = response.json()["run"]
    assert run["status"] == "completed"
    assert run["approval"] is None
    assert len(a2a.calls_for("/agents/hr")) == 1


async def test_expired_approval_cannot_be_decided(client, user_headers, approver_headers, finance, app):
    app.state.container.settings.approval_ttl_minutes = -1
    started = await client.post("/v1/orchestrate", json={"query": WRITE_QUERY}, headers=user_headers)
    approval_id = started.json()["run"]["approval"]["approval_id"]

    decision = await client.post(
        f"/v1/approvals/{approval_id}/decision",
        json={"approved": True},
        headers=approver_headers,
    )
    assert decision.status_code == 409
    assert decision.json()["error"]["code"] == "approval_expired"
# ---------------------------------------------------------------------------
# planner approval opinion must not gate read-only plans
# ---------------------------------------------------------------------------


def _assessment(plan_requires: bool, action, query: str = "", **card_overrides):
    from alstom_ao.approvals.service import assess_approval
    from alstom_ao.config import Settings
    from alstom_ao.domain.models import (
        AgentCard,
        AgentPermissions,
        IdentityContext,
        Plan,
        PlannedAgent,
        ServiceIdentity,
        UserIdentity,
    )

    settings = Settings(environment="test",
        # adapter tests use fake peers directly; production is gateway-only
        a2a_routing="direct", database_url="sqlite+aiosqlite:///:memory:")
    card = AgentCard(
        agent_id="finance-invoice-auditor",
        name="Finance",
        description="Audits invoices and detects duplicates for the finance team.",
        endpoint="http://test/agents/finance",
        capabilities=["invoice audit"],
        permissions=AgentPermissions(
            allowed_roles=["admin"],
            allowed_actions=["read", "query", "analyze", "write"],
            **{"requires_approval_for": ["write", "update", "payment"], **card_overrides},
        ),
        risk_level="high",
    )
    plan = Plan(
        selected_agents=[PlannedAgent(agent_id=card.agent_id, action=action)],
        requires_human_approval=plan_requires,
        approval_reason="auditing may lead to data changes",
    )
    identity = IdentityContext(
        user=UserIdentity(user_id="alice", roles=["user"]), service=ServiceIdentity()
    )
    return assess_approval(
        plan=plan,
        cards={card.agent_id: card},
        decisions=[],
        identity=identity,
        settings=settings,
        query=query,
    )


def test_planner_opinion_does_not_gate_a_read_only_plan():
    from alstom_ao.domain.enums import ActionType

    result = _assessment(plan_requires=True, action=ActionType.QUERY)
    assert result.required is False
    assert result.advisory  # recorded, not enforced


def test_planner_opinion_still_gates_a_consequential_action():
    from alstom_ao.domain.enums import ActionType

    result = _assessment(plan_requires=True, action=ActionType.WRITE)
    assert result.required is True
    assert any("auditing" in reason for reason in result.reasons)


def test_card_rule_still_gates_a_read_only_action():
    """Deterministic rules keep working: a card can demand approval for reads."""

    from alstom_ao.domain.enums import ActionType

    result = _assessment(plan_requires=False, action=ActionType.QUERY, requires_approval_for=["query"])
    assert result.required is True


def test_analyze_action_is_read_only():
    from alstom_ao.domain.enums import ActionType

    assert _assessment(plan_requires=True, action=ActionType.ANALYZE).required is False
# ---------------------------------------------------------------------------
# safety net: the planner must not be able to label a write as read-only
# ---------------------------------------------------------------------------


def test_mislabelled_write_still_requires_approval():
    """Planner says "query" for "create an adjustment task": approval must fire."""

    from alstom_ao.domain.enums import ActionType

    result = _assessment(
        plan_requires=False, action=ActionType.QUERY, query="Create an adjustment task for invoice INV-1"
    )
    assert result.required is True
    assert any("reads as" in note for note in result.advisory)


def test_read_question_about_creating_something_is_not_gated_twice():
    """A genuinely read-only question still completes when the plan says query."""

    from alstom_ao.domain.enums import ActionType

    result = _assessment(
        plan_requires=True, action=ActionType.QUERY, query="Check invoice INV-2024-0912 for duplicate charges"
    )
    assert result.required is False


def test_delete_wording_is_gated_even_if_planner_says_query():
    from alstom_ao.domain.enums import ActionType

    result = _assessment(plan_requires=False, action=ActionType.QUERY, query="Delete the duplicate charge on INV-1")
    assert result.required is True
