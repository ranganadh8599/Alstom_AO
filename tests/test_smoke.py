"""Smoke test: the app boots, the registry works, the fake A2A network is wired."""

from __future__ import annotations

from tests.conftest import agent_payload


async def test_healthz(client):
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_identity_is_required(client):
    response = await client.get("/v1/agents")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_register_and_list_agent(client, admin_headers):
    payload = agent_payload("smoke-agent")
    created = await client.post("/v1/agents", json=payload, headers=admin_headers)
    assert created.status_code == 201, created.text
    assert created.json()["agent"]["agent_id"] == "smoke-agent"

    listed = await client.get("/v1/agents", headers=admin_headers)
    assert listed.status_code == 200
    assert any(item["agent_id"] == "smoke-agent" for item in listed.json()["items"])


async def test_registry_is_admin_only(client, user_headers):
    response = await client.post("/v1/agents", json=agent_payload("nope"), headers=user_headers)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


async def test_validation_rejects_bad_endpoint(client, admin_headers):
    payload = agent_payload("bad-endpoint")
    payload["endpoint"] = "not-a-url"
    response = await client.post("/v1/agents", json=payload, headers=admin_headers)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_input"
