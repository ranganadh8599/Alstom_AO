"""Production-readiness behaviours: idempotent submission and startup guards."""

from __future__ import annotations

import httpx
import pytest

from alstom_ao.config import Settings
from alstom_ao.main import create_app, initialize_app, shutdown_app
from tests.conftest import FakeA2ANetwork, agent_payload

FINANCE = dict(
    agent_id="finance-invoice-auditor",
    endpoint="http://test/agents/finance",
    capabilities=["invoice audit", "duplicate invoice detection"],
    tags=["finance", "invoice"],
    examples=["Check invoice INV-2024-0912 for duplicate charges"],
    allowed_roles=["admin"],
    allowed_actions=["read", "query", "analyze"],
)


@pytest.fixture
async def finance(client, admin_headers):
    response = await client.post("/v1/agents", json=agent_payload(**FINANCE), headers=admin_headers)
    assert response.status_code == 201, response.text


async def test_repeated_submission_reuses_the_first_run(client, user_headers, finance, a2a):
    headers = {**user_headers, "Idempotency-Key": "submit-abc-123"}
    body = {"query": "Check invoice INV-2024-0912 for duplicate charges"}

    first = await client.post("/v1/orchestrate", json=body, headers=headers)
    second = await client.post("/v1/orchestrate", json=body, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["run"]["run_id"] == second.json()["run"]["run_id"]
    assert first.json()["run"]["response"] == second.json()["run"]["response"]
    # The agent must be called exactly once: a retry cannot double-execute.
    assert len(a2a.calls_for("/agents/finance")) == 1


async def test_replay_preserves_agent_results_and_plan(client, user_headers, finance):
    headers = {**user_headers, "Idempotency-Key": "submit-replay-1"}
    body = {"query": "Check invoice INV-2024-0912 for duplicate charges"}

    await client.post("/v1/orchestrate", json=body, headers=headers)
    replay = await client.post("/v1/orchestrate", json=body, headers=headers)
    run = replay.json()["run"]

    assert run["status"] == "completed"
    assert [agent["agent_id"] for agent in run["plan"]["selected_agents"]] == ["finance-invoice-auditor"]
    assert run["agent_results"][0]["agent_id"] == "finance-invoice-auditor"
    assert "finance handled" in run["agent_results"][0]["text"]


async def test_different_keys_create_different_runs(client, user_headers, finance, a2a):
    body = {"query": "Check invoice INV-2024-0912 for duplicate charges"}
    first = await client.post(
        "/v1/orchestrate", json=body, headers={**user_headers, "Idempotency-Key": "key-1"}
    )
    second = await client.post(
        "/v1/orchestrate", json=body, headers={**user_headers, "Idempotency-Key": "key-2"}
    )
    assert first.json()["run"]["run_id"] != second.json()["run"]["run_id"]
    assert len(a2a.calls_for("/agents/finance")) == 2


async def test_idempotency_is_scoped_per_user(client, user_headers, approver_headers, finance):
    body = {"query": "Check invoice INV-2024-0912 for duplicate charges"}
    alice = await client.post(
        "/v1/orchestrate", json=body, headers={**user_headers, "Idempotency-Key": "shared-key"}
    )
    bob = await client.post(
        "/v1/orchestrate", json=body, headers={**approver_headers, "Idempotency-Key": "shared-key"}
    )
    assert alice.json()["run"]["run_id"] != bob.json()["run"]["run_id"]
    assert alice.json()["run"]["trace_id"] != bob.json()["run"]["trace_id"]


async def test_no_key_means_no_deduplication(client, user_headers, finance, a2a):
    body = {"query": "Check invoice INV-2024-0912 for duplicate charges"}
    first = await client.post("/v1/orchestrate", json=body, headers=user_headers)
    second = await client.post("/v1/orchestrate", json=body, headers=user_headers)
    assert first.json()["run"]["run_id"] != second.json()["run"]["run_id"]


async def test_openai_surface_honours_idempotency_key(client, user_headers, finance, a2a):
    headers = {**user_headers, "Idempotency-Key": "chat-key-1"}
    body = {
        "model": "alstom-ao",
        "messages": [{"role": "user", "content": "Check invoice INV-2024-0912 for duplicate charges"}],
    }
    first = await client.post("/v1/chat/completions", json=body, headers=headers)
    second = await client.post("/v1/chat/completions", json=body, headers=headers)

    assert first.json()["alstom_ao"]["run_id"] == second.json()["alstom_ao"]["run_id"]
    assert len(a2a.calls_for("/agents/finance")) == 1


async def test_replay_of_paused_run_returns_pending_approval(client, user_headers, admin_headers, a2a):
    payload = agent_payload(
        "finance-invoice-auditor",
        endpoint="http://test/agents/finance",
        capabilities=["invoice audit", "invoice adjustment"],
        tags=["finance", "invoice"],
        examples=["Create an adjustment task for invoice INV-2024-0912"],
        allowed_roles=["admin"],
        allowed_actions=["read", "query", "analyze", "write"],
        requires_approval_for=["write"],
        risk_level="high",
    )
    assert (await client.post("/v1/agents", json=payload, headers=admin_headers)).status_code == 201

    headers = {**user_headers, "Idempotency-Key": "write-key-1"}
    body = {"query": "Create an adjustment task for invoice INV-2024-0912"}
    first = await client.post("/v1/orchestrate", json=body, headers=headers)
    second = await client.post("/v1/orchestrate", json=body, headers=headers)

    assert first.json()["run"]["status"] == "awaiting_approval"
    assert second.json()["run"]["status"] == "awaiting_approval"
    assert first.json()["run"]["approval"]["approval_id"] == second.json()["run"]["approval"]["approval_id"]
    assert a2a.calls == []


# ---------------------------------------------------------------------------
# production startup guards
# ---------------------------------------------------------------------------


def prod_settings(**overrides) -> Settings:
    base = dict(
        environment="prod",
        database_url="sqlite+aiosqlite:///:memory:",
        seed_demo_agents=False,
        opa_url=None,
        llm_mode="mock",
    )
    base.update(overrides)
    return Settings(**base)


async def test_production_refuses_to_seed_demo_agents():
    network = FakeA2ANetwork()
    app = create_app(prod_settings(seed_demo_agents=True), a2a_transport=network.transport)
    with pytest.raises(RuntimeError) as exc:
        await initialize_app(app)
    assert "AO_SEED_DEMO_AGENTS" in str(exc.value)


async def test_production_starts_when_seeding_is_disabled():
    network = FakeA2ANetwork()
    app = create_app(prod_settings(), a2a_transport=network.transport)
    await initialize_app(app)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/healthz")
            assert response.status_code == 200
            agents = await client.get("/v1/agents", headers={"X-User-Id": "admin", "X-User-Roles": "admin"})
            assert agents.json()["total"] == 0
    finally:
        await shutdown_app(app)


def test_production_rejects_debug_mode():
    with pytest.raises(ValueError):
        prod_settings(debug=True)


# ---------------------------------------------------------------------------
# operational hardening
# ---------------------------------------------------------------------------


async def test_idempotency_key_is_bounded(client, user_headers, finance):
    too_long = "k" * 129
    response = await client.post(
        "/v1/orchestrate",
        json={"query": "Check invoice INV-1"},
        headers={**user_headers, "Idempotency-Key": too_long},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_input"


async def test_conversation_is_capped(client, user_headers, finance):
    turns = [{"role": "user", "content": "hello"} for _ in range(41)]
    response = await client.post(
        "/v1/orchestrate",
        json={"query": "Check invoice INV-1", "conversation": turns},
        headers=user_headers,
    )
    assert response.status_code == 422


async def test_metrics_can_be_gated_by_token(app, client):
    app.state.container.settings.metrics_token = "scrape-secret"

    denied = await client.get("/metrics")
    assert denied.status_code == 403

    allowed = await client.get("/metrics", headers={"X-Metrics-Token": "scrape-secret"})
    assert allowed.status_code == 200
    assert "ao_runs_started" in allowed.text or allowed.text.strip() == ""


async def test_lost_checkpoint_state_is_rejected_not_silently_completed(tmp_path, a2a):
    """A restart without a durable checkpointer must not fake a successful run."""

    database_url = f"sqlite+aiosqlite:///{tmp_path / 'restart.db'}"

    def boot_settings() -> Settings:
        return Settings(
            environment="test",
        # adapter tests use fake peers directly; production is gateway-only
        a2a_routing="direct",
            database_url=database_url,
            seed_demo_agents=False,
            opa_url=None,
            llm_mode="mock",
        )

    payload = agent_payload(
        "finance-invoice-auditor",
        endpoint="http://test/agents/finance",
        capabilities=["invoice adjustment"],
        tags=["finance"],
        examples=["Create an adjustment task for invoice INV-9"],
        allowed_actions=["read", "write"],
        requires_approval_for=["write"],
        risk_level="high",
    )

    # --- first process: pause on approval
    app_one = create_app(boot_settings(), a2a_transport=a2a.transport)
    await initialize_app(app_one)
    try:
        transport = httpx.ASGITransport(app=app_one)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            admin = {"X-User-Id": "admin", "X-User-Roles": "admin"}
            assert (await client.post("/v1/agents", json=payload, headers=admin)).status_code == 201
            started = await client.post(
                "/v1/orchestrate",
                json={"query": "Create an adjustment task for invoice INV-9"},
                headers={"X-User-Id": "alice", "X-User-Roles": "user"},
            )
            run = started.json()["run"]
            assert run["status"] == "awaiting_approval"
            approval_id = run["approval"]["approval_id"]
            run_id = run["run_id"]
    finally:
        await shutdown_app(app_one)

    # --- second process: same database, brand new in-memory checkpointer
    a2a.calls.clear()
    app_two = create_app(boot_settings(), a2a_transport=a2a.transport)
    await initialize_app(app_two)
    try:
        transport = httpx.ASGITransport(app=app_two)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            approver = {"X-User-Id": "bob", "X-User-Roles": "admin"}
            decision = await client.post(
                f"/v1/approvals/{approval_id}/decision", json={"approved": True}, headers=approver
            )
            assert decision.status_code == 409
            assert decision.json()["error"]["code"] == "run_state_unavailable"

            # Nothing may have executed, and the approval must remain decidable
            # once a durable checkpointer is in place.
            assert a2a.calls == []
            approval = await client.get(f"/v1/approvals/{approval_id}", headers=approver)
            assert approval.json()["status"] == "pending"
            stored = await client.get(
                f"/v1/runs/{run_id}", headers={"X-User-Id": "alice", "X-User-Roles": "user"}
            )
            assert stored.json()["status"] == "awaiting_approval"
    finally:
        await shutdown_app(app_two)

