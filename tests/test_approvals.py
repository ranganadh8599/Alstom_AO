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
    allowed_roles=["user", "finance"],
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
    allowed_roles=["user"],
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
