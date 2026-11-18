"""Tiered budget: soft alerts, model downgrade, hard gate, hop/depth caps.

Design under test (see docs/budgets.md):

* WARN at the soft threshold -> trace event + webhook alert + optional model
  downgrade, and the run keeps going
* EXCEEDED blocks *new* work at the graph boundary (429 at the API), while agent
  results already collected are never thrown away for lack of a synthesis call
* hops and inbound A2A depth are capped independently of dollars
"""

from __future__ import annotations

import json

import httpx
import pytest

from alstom_ao.config import Settings
from alstom_ao.llm.gateway import BudgetDecision, BudgetState, LLMBudget
from alstom_ao.main import create_app, initialize_app, shutdown_app
from alstom_ao.observability import alerts
from tests.conftest import agent_payload

FINANCE = dict(
    agent_id="finance-invoice-auditor",
    endpoint="http://test/agents/finance",
    capabilities=["invoice audit", "duplicate invoice detection"],
    tags=["finance", "invoice"],
    examples=["Check invoice INV-2024-0912 for duplicate charges"],
    allowed_roles=["admin"],
    allowed_actions=["read", "query", "analyze"],
    risk_level="low",
)

QUERY = {"query": "Check invoice INV-2024-0912 for duplicate charges"}


# ---------------------------------------------------------------------------
# unit: decision semantics
# ---------------------------------------------------------------------------


def test_budget_is_ok_below_soft_threshold():
    budget = LLMBudget(max_calls=10, max_tokens=0, max_cost_usd=0, soft_threshold=0.8)
    budget.calls = 5
    decision = budget.evaluate()
    assert decision.state is BudgetState.OK
    assert decision.can_start_new_work


def test_budget_warns_at_soft_threshold():
    budget = LLMBudget(max_calls=10, max_tokens=0, max_cost_usd=0, soft_threshold=0.8)
    budget.calls = 8
    decision = budget.evaluate()
    assert decision.state is BudgetState.WARN
    assert decision.dimension == "llm_calls"
    assert decision.percent == pytest.approx(0.8)
    assert decision.should_degrade
    assert decision.can_start_new_work


def test_budget_hard_exceed_still_reports_dimension():
    budget = LLMBudget(max_calls=10, max_tokens=0, max_cost_usd=0)
    budget.calls = 10
    decision = budget.evaluate()
    assert decision.state is BudgetState.EXCEEDED
    assert decision.dimension == "llm_calls"
    assert not decision.can_start_new_work


def test_hop_dimension_is_part_of_the_budget():
    budget = LLMBudget(max_calls=0, max_tokens=0, max_cost_usd=0, max_hops=2)
    budget.record_hop()
    assert budget.evaluate().state is BudgetState.OK
    budget.record_hop()
    decision = budget.evaluate()
    assert decision.state is BudgetState.EXCEEDED
    assert decision.dimension == "agent_hops"


def test_warning_claimed_once_per_dimension():
    budget = LLMBudget(max_calls=10, max_tokens=0, max_cost_usd=0)
    budget.calls = 9
    decision = budget.evaluate()
    assert budget.claim_warning(decision) is True
    assert budget.claim_warning(decision) is False


def test_budget_round_trips_hops_through_snapshot():
    budget = LLMBudget(max_calls=10, max_tokens=100, max_cost_usd=1, max_hops=5)
    budget.record_hop()
    budget.record_hop()
    settings = Settings(environment="test",
        litellm_proxy_url="http://test",
        litellm_proxy_api_key="sk-test-gateway-key", database_url="sqlite+aiosqlite:///:memory:")
    restored = LLMBudget.from_snapshot(budget.snapshot(), settings)
    assert restored.hops == 2


# ---------------------------------------------------------------------------
# gateway: downgrade
# ---------------------------------------------------------------------------


async def test_gateway_downgrade_switches_model(settings):
    from alstom_ao.llm.gateway import ScriptedGateway

    budget = LLMBudget(max_calls=5, max_tokens=0, max_cost_usd=0)
    gateway = ScriptedGateway(settings, budget, responses=["ok"])
    assert gateway.current_model() == settings.llm_model
    assert gateway.downgrade("openai/gpt-4o-mini-cheap") is True
    assert gateway.current_model() == "openai/gpt-4o-mini-cheap"
    # Downgrading to the same model is a no-op, not an error.
    assert gateway.downgrade("openai/gpt-4o-mini-cheap") is False


# ---------------------------------------------------------------------------
# alerts
# ---------------------------------------------------------------------------


def test_alert_payload_shape():
    decision = BudgetDecision(
        state=BudgetState.WARN, dimension="cost_usd", used=0.4, limit=0.5, percent=0.8
    )
    payload = alerts.build_payload(
        decision=decision,
        run_id="run_1",
        workflow_id="wf_1",
        user_id="alice",
        environment="prod",
        model="openai/gpt-4o-mini",
    )
    assert payload["event"] == "budget.warning"
    assert payload["percent"] == 0.8
    assert "[prod] Budget approaching" in payload["text"]
    json.dumps(payload)  # must be serializable


async def test_emit_alert_posts_and_never_raises(monkeypatch):
    sent: list[dict] = []

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None):
            sent.append({"url": url, "payload": json})
            return httpx.Response(200)

    monkeypatch.setattr(alerts, "AsyncClient", FakeClient)
    settings = Settings(
        environment="test",
        litellm_proxy_url="http://test",
        litellm_proxy_api_key="sk-test-gateway-key",
        database_url="sqlite+aiosqlite:///:memory:",
        budget_alert_webhook="https://hooks.example/budget",
    )
    decision = BudgetDecision(state=BudgetState.WARN, dimension="tokens", used=80, limit=100, percent=0.8)
    assert alerts.emit_alert(
        settings,
        decision=decision,
        run_id="run_1",
        workflow_id="wf_1",
        user_id="alice",
        environment="test",
        model="m",
    )
    await alerts.drain()
    assert sent and sent[0]["url"] == "https://hooks.example/budget"
    assert sent[0]["payload"]["event"] == "budget.warning"


def test_emit_alert_is_a_noop_without_webhook(settings):
    decision = BudgetDecision(state=BudgetState.WARN, dimension="tokens", used=80, limit=100, percent=0.8)
    assert not alerts.emit_alert(
        settings,
        decision=decision,
        run_id="r",
        workflow_id="w",
        user_id="u",
        environment="test",
        model="m",
    )


# ---------------------------------------------------------------------------
# end to end
# ---------------------------------------------------------------------------


@pytest.fixture
async def budget_app(a2a, request):
    """App whose settings are tuned per test via ``request.param``."""

    overrides = getattr(request, "param", {})
    settings = Settings(
        environment="test",
        litellm_proxy_url="http://test",
        litellm_proxy_api_key="sk-test-gateway-key",
        database_url="sqlite+aiosqlite:///:memory:",
        seed_demo_agents=False,
        opa_url=None,
        llm_mode="mock",
        **overrides,
    )
    app = create_app(settings, a2a_transport=a2a.transport)
    await initialize_app(app)
    try:
        yield app
    finally:
        await shutdown_app(app)


async def _register_and_run(app, headers) -> dict:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        admin = {"X-User-Id": "admin", "X-User-Roles": "admin"}
        await client.post("/v1/agents", json=agent_payload(**FINANCE), headers=admin)
        response = await client.post("/v1/orchestrate", json=QUERY, headers=headers)
        return {"status_code": response.status_code, "body": response.json()}


@pytest.fixture
def scripted_gateway(monkeypatch):
    """Replace the mock gateway with one that actually bills calls.

    The mock gateway short-circuits planning, so budget accounting would never
    move. A scripted gateway returns unparsable JSON (planner falls back to the
    heuristic) while charging every call, which is what these tiers respond to.
    """

    from alstom_ao.llm.gateway import ScriptedGateway

    def fake_build(settings, budget, *, responses=None):
        return ScriptedGateway(settings, budget, responses=["{}"] * 200)

    monkeypatch.setattr("alstom_ao.orchestrator.service.build_gateway", fake_build)


@pytest.mark.parametrize("budget_app", [{"max_agent_hops": 1}], indirect=True)
async def test_hop_cap_drops_lowest_priority_agents(budget_app, user_headers, a2a):
    """Two matching agents, one hop: the plan is truncated, not the budget."""

    transport = httpx.ASGITransport(app=budget_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        admin = {"X-User-Id": "admin", "X-User-Roles": "admin"}
        for agent_id in ("finance-invoice-auditor", "finance-invoice-auditor-2"):
            payload = {**agent_payload(**{**FINANCE, "agent_id": agent_id})}
            await client.post("/v1/agents", json=payload, headers=admin)
        response = await client.post("/v1/orchestrate", json=QUERY, headers=user_headers)

    run = response.json()["run"]
    assert run["status"] == "completed"
    assert len(run["plan"]["selected_agents"]) == 1
    assert len(a2a.calls_for("/agents/finance")) == 1

    trace = await _trace(budget_app, run_id=run["run_id"], headers=user_headers)
    hop_events = [event for event in trace if event["action"] == "hop.limit_hit"]
    assert hop_events, [event["action"] for event in trace]
    assert hop_events[0]["status"] == "skipped"


@pytest.mark.parametrize("budget_app", [{"llm_max_calls_per_run": 1}], indirect=True)
async def test_exhausted_budget_blocks_agents(
    budget_app, user_headers, a2a, scripted_gateway
):
    """Planning consumes the only allowed call; execution must not invoke agents."""

    result = await _register_and_run(budget_app, user_headers)
    run = result["body"]["run"]
    assert run["status"] == "failed"
    assert "budget" in run["response"].lower()
    assert a2a.calls == []

    trace = await _trace(budget_app, run_id=run["run_id"], headers=user_headers)
    actions = [event["action"] for event in trace]
    assert "budget.exceeded" in actions, actions
    assert "agent.invoked" not in actions


@pytest.mark.parametrize(
    "budget_app",
    [{"llm_max_calls_per_run": 2, "budget_soft_threshold": 0.5}],
    indirect=True,
)
async def test_soft_threshold_degrades_instead_of_failing(
    budget_app, user_headers, a2a, monkeypatch, scripted_gateway
):
    """Crossing the soft line alerts and downgrades; the run still completes."""

    fired: list[dict] = []

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None):
            fired.append(json)
            return httpx.Response(200)

    monkeypatch.setattr(alerts, "AsyncClient", FakeClient)
    budget_app.state.container.settings.budget_alert_webhook = "https://hooks.example/budget"
    budget_app.state.container.settings.budget_degrade_model = "openai/gpt-4o-mini-cheap"

    result = await _register_and_run(budget_app, user_headers)
    run = result["body"]["run"]
    assert run["status"] == "completed", run["response"]

    trace = await _trace(budget_app, run_id=run["run_id"], headers=user_headers)
    actions = [event["action"] for event in trace]
    assert "budget.degraded" in actions, actions

    await alerts.drain()
    assert fired and fired[0]["event"] == "budget.warning"
    assert fired[0]["model"] == "openai/gpt-4o-mini-cheap"


@pytest.mark.parametrize("budget_app", [{"max_a2a_depth": 2}], indirect=True)
async def test_inbound_depth_limit_blocks_fan_out(budget_app, user_headers, a2a):
    transport = httpx.ASGITransport(app=budget_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        admin = {"X-User-Id": "admin", "X-User-Roles": "admin"}
        await client.post("/v1/agents", json=agent_payload(**FINANCE), headers=admin)
        response = await client.post(
            "/v1/orchestrate", json=QUERY, headers={**user_headers, "X-Alstom-Depth": "2"}
        )
    run = response.json()["run"]
    assert run["status"] == "failed"
    assert "delegation limit was reached" in run["response"]
    assert a2a.calls == []


@pytest.mark.parametrize("budget_app", [{"max_a2a_depth": 3}], indirect=True)
async def test_depth_below_limit_is_allowed(budget_app, user_headers, a2a):
    transport = httpx.ASGITransport(app=budget_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        admin = {"X-User-Id": "admin", "X-User-Roles": "admin"}
        await client.post("/v1/agents", json=agent_payload(**FINANCE), headers=admin)
        response = await client.post(
            "/v1/orchestrate", json=QUERY, headers={**user_headers, "X-Alstom-Depth": "1"}
        )
    assert response.json()["run"]["status"] == "completed"

async def _trace(app, *, run_id: str, headers: dict) -> list[dict]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/v1/runs/{run_id}/trace", headers=headers)
        assert response.status_code == 200, response.text
        return response.json()["events"]
