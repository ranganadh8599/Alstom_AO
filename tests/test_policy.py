"""Policy evaluation: local rules, observe/enforce semantics, OPA integration."""

from __future__ import annotations

import json

import httpx

from alstom_ao.config import Settings
from alstom_ao.domain.enums import ActionType
from alstom_ao.domain.models import AgentCard, IdentityContext, ServiceIdentity, UserIdentity
from alstom_ao.policy.service import OPAClient, PolicyService, local_decision
from tests.conftest import agent_payload


def make_card(**overrides) -> AgentCard:
    payload = agent_payload("policy-agent", **overrides)
    return AgentCard(**payload)


def make_identity(
    *,
    user_id: str = "alice",
    roles: list[str] | None = None,
    tenant: str | None = "tenant-a",
    scopes: list[str] | None = None,
) -> IdentityContext:
    return IdentityContext(
        user=UserIdentity(user_id=user_id, roles=roles or ["user", "finance"], tenant_id=tenant),
        service=ServiceIdentity(service_id="alstom-ao", scopes=scopes or []),
    )


def settings(**overrides) -> Settings:
    base = dict(
        environment="test",
        database_url="sqlite+aiosqlite:///:memory:",
        opa_url=None,
        policy_mode="observe",
        policy_local_fallback=True,
    )
    base.update(overrides)
    return Settings(**base)


def test_allows_permitted_read():
    decision = local_decision(
        identity=make_identity(),
        agent=make_card(),
        action=ActionType.QUERY,
        environment="test",
    )
    assert decision.allowed is True
    assert decision.reasons == []


def test_denies_environment_mismatch():
    decision = local_decision(
        identity=make_identity(),
        agent=make_card(environment="prod"),
        action=ActionType.QUERY,
        environment="test",
    )
    assert decision.allowed is False
    assert "environment_mismatch" in decision.reasons


def test_denies_role_and_action():
    decision = local_decision(
        identity=make_identity(roles=["user"]),
        agent=make_card(allowed_roles=["admin"], allowed_actions=["read"]),
        action=ActionType.QUERY,
        environment="test",
    )
    assert decision.allowed is False
    assert "role_not_permitted" in decision.reasons
    assert "action_not_permitted" in decision.reasons


def test_denies_missing_service_scope():
    decision = local_decision(
        identity=make_identity(scopes=[]),
        agent=make_card(required_scopes=["finance.write"]),
        action=ActionType.QUERY,
        environment="test",
    )
    assert "service_scope_missing" in decision.reasons


def test_denies_restricted_data_without_steward_role():
    card = make_card()
    card.permissions.data_classification = "restricted"
    decision = local_decision(
        identity=make_identity(roles=["user"]),
        agent=card,
        action=ActionType.QUERY,
        environment="test",
    )
    assert "restricted_data_role_missing" in decision.reasons


def test_high_risk_write_requires_approval_but_read_does_not():
    card = make_card(risk_level="high", requires_approval_for=["write"])
    write = local_decision(
        identity=make_identity(), agent=card, action=ActionType.WRITE, environment="test"
    )
    read = local_decision(
        identity=make_identity(), agent=card, action=ActionType.QUERY, environment="test"
    )
    assert write.requires_approval is True
    assert read.requires_approval is False


def test_admin_bypasses_environment_check():
    decision = local_decision(
        identity=make_identity(roles=["admin"]),
        agent=make_card(environment="prod"),
        action=ActionType.QUERY,
        environment="test",
    )
    assert decision.allowed is True


async def test_observe_mode_records_denial_without_blocking():
    service = PolicyService(settings(policy_mode="observe"))
    decision = await service.evaluate_agent(
        identity=make_identity(roles=["user"]),
        agent=make_card(allowed_roles=["admin"]),
        action=ActionType.QUERY,
        environment="test",
        use_case="test",
        query="q",
    )
    assert decision.allowed is True
    assert "role_not_permitted" in decision.reasons
    assert "denied_in_observe_mode" in decision.reasons


async def test_enforce_mode_blocks_when_local_fallback_disabled():
    service = PolicyService(settings(policy_mode="enforce", policy_local_fallback=False))
    decision = await service.evaluate_agent(
        identity=make_identity(),
        agent=make_card(),
        action=ActionType.QUERY,
        environment="test",
        use_case="test",
        query="q",
    )
    assert decision.allowed is False
    assert decision.source == "opa-unavailable"


async def test_opa_decision_is_used_when_available():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.url.path == "/v1/data/alstom/authz/decision"
        assert body["input"]["agent"]["agent_id"] == "policy-agent"
        return httpx.Response(
            200,
            json={"result": {"allow": False, "reasons": ["opa_says_no"], "obligations": {"requires_approval": True}}},
        )

    opa = OPAClient(settings(opa_url="http://opa"), transport=httpx.MockTransport(handler))
    service = PolicyService(settings(policy_mode="enforce", opa_url="http://opa"), opa=opa)
    decision = await service.evaluate_agent(
        identity=make_identity(),
        agent=make_card(),
        action=ActionType.QUERY,
        environment="test",
        use_case="test",
        query="q",
    )
    assert decision.allowed is False
    assert decision.source == "opa"
    assert decision.reasons == ["opa_says_no"]
    assert decision.requires_approval is True


async def test_policy_api_dry_run(client, user_headers, admin_headers):
    created = await client.post("/v1/agents", json=agent_payload("dry-run-agent"), headers=admin_headers)
    assert created.status_code == 201

    response = await client.post(
        "/v1/policies/evaluate",
        json={"agent_id": "dry-run-agent", "action": "query", "environment": "test"},
        headers=user_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["allowed"] is True
    assert body["source"] in {"local-fallback", "local"}

    status = await client.get("/v1/policies/status", headers=user_headers)
    assert status.status_code == 200
    assert status.json()["mode"] == "observe"
