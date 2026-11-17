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
    """A plain user is not offered an admin-only agent; an admin is."""

    await client.post(
        "/v1/agents", json=agent_payload("admin-only-agent", allowed_roles=["admin"]), headers=admin_headers
    )
    await client.post(
        "/v1/agents", json=agent_payload("user-agent", allowed_roles=["user"]), headers=admin_headers
    )

    as_user = await client.get("/v1/agents/catalog", headers=user_headers)
    ids = [item["agent_id"] for item in as_user.json()["agents"]]
    assert "user-agent" in ids
    assert "admin-only-agent" not in ids

    as_admin = await client.get("/v1/agents/catalog", headers=admin_headers)
    admin_ids = [item["agent_id"] for item in as_admin.json()["agents"]]
    assert "admin-only-agent" in admin_ids
# ---------------------------------------------------------------------------
# catalog must not empty out on an unmapped role
# ---------------------------------------------------------------------------


async def test_unmapped_role_returns_unfiltered_catalog(client, admin_headers):
    """A UI sending an application role (ADMIN) must not see "no agents"."""

    await client.post("/v1/agents", json=agent_payload("finance-agent", allowed_roles=["admin"]), headers=admin_headers)

    ui_headers = {"X-User-Id": "6afcce251a37b84260e76d33", "X-User-Roles": "ADMIN"}
    catalog = await client.get("/v1/agents/catalog", headers=ui_headers)
    assert catalog.status_code == 200
    body = catalog.json()
    assert body["total"] >= 1
    assert any(item["agent_id"] == "finance-agent" for item in body["agents"])


async def test_admin_only_agent_is_hidden_from_plain_users(client, admin_headers):
    """Filtering still works: an admin-only agent is not offered to a user."""

    await client.post(
        "/v1/agents", json=agent_payload("admin-only-agent", allowed_roles=["admin"]), headers=admin_headers
    )
    await client.post("/v1/agents", json=agent_payload("shared-agent"), headers=admin_headers)

    as_user = await client.get("/v1/agents/catalog", headers={"X-User-Id": "u1", "X-User-Roles": "user"})
    ids = [item["agent_id"] for item in as_user.json()["agents"]]
    assert "shared-agent" in ids
    assert "admin-only-agent" not in ids

    as_admin = await client.get("/v1/agents/catalog", headers={"X-User-Id": "a1", "X-User-Roles": "admin"})
    admin_ids = [item["agent_id"] for item in as_admin.json()["agents"]]
    assert "admin-only-agent" in admin_ids
