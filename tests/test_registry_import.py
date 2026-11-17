"""Registry features beyond CRUD: A2A import and health probing."""

from __future__ import annotations

from tests.conftest import agent_payload


async def test_discovers_and_imports_an_a2a_agent(client, admin_headers, a2a):
    response = await client.post(
        "/v1/agents/discover",
        json={"endpoint": "http://test/agents/hr", "environment": "test"},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    card = response.json()["agent"]

    assert card["agent_id"] == "hr-agent"
    assert card["protocol"] == "a2a"
    assert card["endpoint"] == "http://test/agents/hr"
    assert card["capabilities"] == ["HR Agent"]
    assert card["owner"] == "Alstom Demo" or card["owner"] == ""
    assert card["environment"] == "test"

    fetched = await client.get(f"/v1/agents/{card['agent_id']}", headers=admin_headers)
    assert fetched.status_code == 200


async def test_health_probe_updates_card(client, admin_headers, a2a):
    created = await client.post(
        "/v1/agents",
        json=agent_payload("probe-agent", endpoint="http://test/agents/it"),
        headers=admin_headers,
    )
    assert created.status_code == 201

    healthy = await client.post("/v1/agents/probe-agent/health", headers=admin_headers)
    assert healthy.status_code == 200
    assert healthy.json()["health"] == "healthy"

    a2a._cards.pop("http://test/agents/it", None)
    unhealthy = await client.post("/v1/agents/probe-agent/health", headers=admin_headers)
    assert unhealthy.json()["health"] == "unhealthy"


async def test_catalog_is_role_filtered(client, user_headers, admin_headers):
    await client.post(
        "/v1/agents",
        json=agent_payload("finance-only-agent", allowed_roles=["finance-manager"]),
        headers=admin_headers,
    )
    await client.post(
        "/v1/agents",
        json=agent_payload("user-agent", allowed_roles=["user"]),
        headers=admin_headers,
    )
    catalog = await client.get("/v1/agents/catalog", headers=user_headers)
    ids = [item["agent_id"] for item in catalog.json()["agents"]]
    assert "user-agent" in ids
    assert "finance-only-agent" not in ids
