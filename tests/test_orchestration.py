"""End-to-end orchestration: routing, A2A fan-out, trace ledger, graceful no-match."""

from __future__ import annotations

from typing import Any

import pytest

from tests.conftest import agent_payload

FINANCE = dict(
    agent_id="finance-invoice-auditor",
    endpoint="http://test/agents/finance",
    capabilities=["invoice audit", "duplicate invoice detection", "vendor payment status"],
    tags=["finance", "invoice", "vendor"],
    examples=["Check invoice INV-2024-0912 for duplicate charges"],
    allowed_roles=["admin"],
    allowed_actions=["read", "query", "analyze", "write"],
    requires_approval_for=["write", "update", "payment"],
    risk_level="high",
)
HR = dict(
    agent_id="hr-policy-advisor",
    endpoint="http://test/agents/hr",
    capabilities=["hr policy lookup", "leave and absence rules", "travel policy"],
    tags=["hr", "policy", "leave"],
    examples=["How many days of annual leave do I get after three years?"],
    allowed_roles=["admin"],
    allowed_actions=["read", "query", "analyze"],
    risk_level="low",
)
IT = dict(
    agent_id="it-service-desk",
    endpoint="http://test/agents/it",
    capabilities=["vpn troubleshooting", "software catalog", "laptop provisioning"],
    tags=["it", "vpn", "access"],
    examples=["How do I connect to the VPN from a Mac?"],
    allowed_roles=["admin"],
    allowed_actions=["read", "query", "analyze", "write"],
    requires_approval_for=["write"],
    risk_level="medium",
)


async def register(client, headers, **overrides) -> dict[str, Any]:
    payload = agent_payload(**overrides)
    response = await client.post("/v1/agents", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["agent"]


@pytest.fixture
async def agents(client, admin_headers):
    return {
        "finance": await register(client, admin_headers, **FINANCE),
        "hr": await register(client, admin_headers, **HR),
        "it": await register(client, admin_headers, **IT),
    }


async def test_routes_invoice_question_to_finance(client, user_headers, agents, a2a):
    response = await client.post(
        "/v1/orchestrate",
        json={"query": "Check invoice INV-2024-0912 for duplicate charges"},
        headers=user_headers,
    )
    assert response.status_code == 200, response.text
    run = response.json()["run"]

    assert run["status"] == "completed"
    assert [agent["agent_id"] for agent in run["plan"]["selected_agents"]] == ["finance-invoice-auditor"]
    assert "finance handled" in run["response"]
    assert run["agent_results"][0]["agent_version"] == "1.0.0"
    assert run["agent_results"][0]["protocol"] == "a2a"
    assert len(a2a.calls_for("/agents/finance")) == 1


async def test_routes_hr_and_it_queries(client, user_headers, agents, a2a):
    hr = await client.post(
        "/v1/orchestrate",
        json={"query": "How many days of annual leave do I get after three years?"},
        headers=user_headers,
    )
    assert hr.json()["run"]["plan"]["selected_agents"][0]["agent_id"] == "hr-policy-advisor"

    it = await client.post(
        "/v1/orchestrate",
        json={"query": "How do I connect to the VPN from a Mac?"},
        headers=user_headers,
    )
    assert it.json()["run"]["plan"]["selected_agents"][0]["agent_id"] == "it-service-desk"
    assert len(a2a.calls_for("/agents/hr")) == 1
    assert len(a2a.calls_for("/agents/it")) == 1


async def test_no_match_returns_helpful_message(client, user_headers, agents, a2a):
    response = await client.post(
        "/v1/orchestrate",
        json={"query": "What is the airspeed velocity of an unladen swallow?"},
        headers=user_headers,
    )
    run = response.json()["run"]
    assert run["status"] == "no_match"
    assert "couldn't find a registered agent" in run["response"]
    assert "Finance" in run["response"] or "finance" in run["response"]
    assert a2a.calls == []


async def test_empty_query_is_rejected(client, user_headers, agents):
    response = await client.post("/v1/orchestrate", json={"query": "   "}, headers=user_headers)
    assert response.status_code == 422


async def test_trace_ledger_carries_dual_identity_and_agent_versions(
    client, user_headers, agents, a2a
):
    response = await client.post(
        "/v1/orchestrate",
        json={"query": "Check invoice INV-2024-0912 for duplicate charges"},
        headers=user_headers,
    )
    run = response.json()["run"]

    trace = await client.get(f"/v1/runs/{run['run_id']}/trace", headers=user_headers)
    assert trace.status_code == 200
    events = trace.json()["events"]
    actions = [event["action"] for event in events]

    for expected in (
        "run.started",
        "input.validated",
        "plan.created",
        "policy.evaluated",
        "agent.invoked",
        "agent.completed",
        "synthesis.completed",
        "run.completed",
    ):
        assert expected in actions, actions

    agent_events = [event for event in events if event["agent_id"] == "finance-invoice-auditor"]
    assert agent_events
    for event in agent_events:
        assert event["agent_version"] == "1.0.0"
        assert event["protocol"] == "a2a"
        assert event["initiated_by"] == "alice"
        assert event["authenticated_service"] == "alstom-ao"
        assert event["trace_id"] == run["trace_id"]

    # The downstream agent received the full trace context and both identities.
    call = a2a.calls_for("/agents/finance")[0]
    trace_context = call["metadata"]["trace_context"]
    assert trace_context["trace_id"] == run["trace_id"]
    assert trace_context["workflow_id"] == run["workflow_id"]
    assert trace_context["run_id"] == run["run_id"]
    assert trace_context["agent_id"] == "finance-invoice-auditor"
    assert trace_context["agent_version"] == "1.0.0"
    assert trace_context["initiated_by"] == "alice"
    assert trace_context["authenticated_service"] == "alstom-ao"
    assert call["metadata"]["identity"]["user"]["user_id"] == "alice"
    assert call["headers"]["x-alstom-trace-id"] == run["trace_id"]
    assert call["headers"]["x-alstom-agent-id"] == "finance-invoice-auditor"


async def test_timeline_reports_agent_order(client, user_headers, agents):
    response = await client.post(
        "/v1/orchestrate",
        json={"query": "Check invoice INV-2024-0912 for duplicate charges"},
        headers=user_headers,
    )
    run = response.json()["run"]
    timeline = await client.get(f"/v1/runs/{run['run_id']}/timeline", headers=user_headers)
    assert timeline.status_code == 200
    body = timeline.json()
    assert body["status"] == "completed"
    assert body["agents"][0]["agent_id"] == "finance-invoice-auditor"


async def test_partial_agent_failure_still_answers(client, user_headers, agents, a2a):
    a2a.fail_paths.add("/agents/finance")
    response = await client.post(
        "/v1/orchestrate",
        json={"query": "Check invoice INV-2024-0912 for duplicate charges"},
        headers=user_headers,
    )
    run = response.json()["run"]
    assert run["status"] == "failed"
    assert "couldn't complete" in run["response"]
    events = (await client.get(f"/v1/runs/{run['run_id']}/trace", headers=user_headers)).json()["events"]
    assert any(event["action"] == "agent.failed" for event in events)


async def test_policy_denies_agent_in_enforce_mode(client, user_headers, admin_headers, a2a, app):
    await register(
        client,
        admin_headers,
        agent_id="restricted-agent",
        endpoint="http://test/agents/finance",
        capabilities=["invoice audit", "duplicate invoice detection"],
        tags=["finance", "invoice"],
        examples=["Check invoice INV-1 for duplicate charges"],
        allowed_roles=["admin"],
        allowed_actions=["read", "query"],
        required_scopes=["finance.write"],
    )
    app.state.container.settings.policy_mode = "enforce"

    response = await client.post(
        "/v1/orchestrate",
        json={"query": "Check invoice INV-999 for duplicate charges"},
        headers=user_headers,
    )
    run = response.json()["run"]
    assert run["status"] == "denied"
    assert "Access denied" in run["response"]
    assert a2a.calls_for("/agents/finance") == []

    decisions = await client.get(f"/v1/runs/{run['run_id']}/policy", headers=user_headers)
    assert decisions.status_code == 200
    assert decisions.json()["decisions"][0]["allowed"] is False
    assert "service_scope_missing" in decisions.json()["decisions"][0]["reasons"]
# Multi-intent queries must fan out to every agent that owns part of the answer.
# ---------------------------------------------------------------------------


def _assessment_unused():  # keeps the module import order stable for the new block
    return None


MULTI = "I need help with my holiday entitlement and also my VPN access on a Mac"
THREE = (
    "How many days annual leave do I get after three years, what is the VPN setup for Mac, "
    "and are there duplicate charges on invoice INV-2024-0912?"
)


async def _seed_three(client, admin_headers):
    from tests.conftest import agent_payload

    spec = {
        "hr-policy-advisor": dict(
            endpoint="http://test/agents/hr",
            capabilities=["leave and absence rules", "hr policy lookup"],
            tags=["hr", "leave"],
            examples=["How many days of annual leave do I get?"],
        ),
        "it-service-desk": dict(
            endpoint="http://test/agents/it",
            capabilities=["vpn troubleshooting", "laptop provisioning"],
            tags=["it", "vpn"],
            examples=["How do I connect to the VPN from a Mac?"],
        ),
        "finance-invoice-auditor": dict(
            endpoint="http://test/agents/finance",
            capabilities=["invoice audit", "duplicate invoice detection"],
            tags=["finance", "invoice"],
            examples=["Check invoice INV-1 for duplicate charges"],
        ),
    }
    for agent_id, overrides in spec.items():
        body = agent_payload(agent_id, **overrides)
        await client.post("/v1/agents", json=body, headers=admin_headers)


async def test_two_intents_calls_two_agents(client, admin_headers, user_headers, a2a):
    await _seed_three(client, admin_headers)
    response = await client.post("/v1/orchestrate", json={"query": MULTI}, headers=user_headers)
    run = response.json()["run"]

    assert run["status"] == "completed"
    agents = [a["agent_id"] for a in run["agent_results"]]
    assert "hr-policy-advisor" in agents
    assert "it-service-desk" in agents
    assert len(agents) == 2
    # both agents were really contacted
    assert len(a2a.calls_for("/agents/hr")) == 1
    assert len(a2a.calls_for("/agents/it")) == 1


async def test_three_intents_calls_three_agents(client, admin_headers, user_headers, a2a):
    await _seed_three(client, admin_headers)
    response = await client.post("/v1/orchestrate", json={"query": THREE}, headers=user_headers)
    run = response.json()["run"]

    assert run["status"] == "completed"
    agents = {a["agent_id"] for a in run["agent_results"]}
    assert agents == {"hr-policy-advisor", "it-service-desk", "finance-invoice-auditor"}
    assert len(a2a.calls_for("/agents/hr")) == 1
    assert len(a2a.calls_for("/agents/it")) == 1
    assert len(a2a.calls_for("/agents/finance")) == 1


async def test_single_intent_does_not_over_fan_out(client, admin_headers, user_headers, a2a):
    await _seed_three(client, admin_headers)
    response = await client.post(
        "/v1/orchestrate", json={"query": "How do I connect to the VPN from a Mac?"}, headers=user_headers
    )
    agents = [a["agent_id"] for a in response.json()["run"]["agent_results"]]
    assert agents == ["it-service-desk"]


async def test_multi_intent_trace_records_fan_out(client, admin_headers, user_headers, a2a):
    await _seed_three(client, admin_headers)
    response = await client.post("/v1/orchestrate", json={"query": THREE}, headers=user_headers)
    run = response.json()["run"]

    trace = await client.get(f"/v1/runs/{run['run_id']}/trace", headers=user_headers)
    events = trace.json()["events"]
    actions = [e["action"] for e in events]
    assert actions.count("agent.invoked") == 3
    assert actions.count("agent.completed") == 3
    assert "agents.fan_in" in actions

    invoked = [e for e in events if e["action"] == "agent.invoked"]
    first, last = invoked[0], invoked[-1]
    assert first["input"]["previous_agents"] == []
    assert len(last["input"]["previous_agents"]) == 2
