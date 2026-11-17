"""Access control: a caller must not read another tenant's runs or approvals."""

from __future__ import annotations

from tests.conftest import agent_payload

FINANCE = dict(
    agent_id="finance-invoice-auditor",
    endpoint="http://test/agents/finance",
    capabilities=["invoice audit", "duplicate invoice detection"],
    tags=["finance", "invoice"],
    examples=["Check invoice INV-2024-0912 for duplicate charges"],
    allowed_roles=["user", "finance"],
    allowed_actions=["read", "query", "analyze"],
    risk_level="low",
)

ALICE = {"X-User-Id": "alice", "X-User-Roles": "user,finance", "X-Tenant-Id": "tenant-A"}
MALLORY = {"X-User-Id": "mallory", "X-User-Roles": "user", "X-Tenant-Id": "tenant-B"}
BOB_SAME_TENANT_APPROVER = {
    "X-User-Id": "bob",
    "X-User-Roles": "approver",
    "X-Tenant-Id": "tenant-A",
}
CAROL_OTHER_TENANT_APPROVER = {
    "X-User-Id": "carol",
    "X-User-Roles": "approver",
    "X-Tenant-Id": "tenant-B",
}


async def _make_run(client, admin_headers) -> dict:
    created = await client.post("/v1/agents", json=agent_payload(**FINANCE), headers=admin_headers)
    assert created.status_code == 201, created.text
    response = await client.post(
        "/v1/orchestrate",
        json={"query": "Check invoice INV-2024-0912 for duplicate charges"},
        headers=ALICE,
    )
    assert response.status_code == 200, response.text
    return response.json()["run"]


async def test_owner_can_read_own_run(client, admin_headers):
    run = await _make_run(client, admin_headers)
    for path in (f"/v1/runs/{run['run_id']}", f"/v1/runs/{run['run_id']}/trace",
                 f"/v1/runs/{run['run_id']}/timeline", f"/v1/runs/{run['run_id']}/policy"):
        response = await client.get(path, headers=ALICE)
        assert response.status_code == 200, path


async def test_cross_tenant_user_cannot_read_run(client, admin_headers):
    run = await _make_run(client, admin_headers)
    for path in (f"/v1/runs/{run['run_id']}", f"/v1/runs/{run['run_id']}/trace",
                 f"/v1/runs/{run['run_id']}/timeline", f"/v1/runs/{run['run_id']}/policy"):
        response = await client.get(path, headers=MALLORY)
        assert response.status_code == 403, f"{path} leaked"
        assert response.json()["error"]["code"] == "forbidden"


async def test_cross_tenant_approver_cannot_read_run(client, admin_headers):
    run = await _make_run(client, admin_headers)
    response = await client.get(f"/v1/runs/{run['run_id']}", headers=CAROL_OTHER_TENANT_APPROVER)
    assert response.status_code == 403


async def test_same_tenant_approver_can_review_colleague_run(client, admin_headers):
    run = await _make_run(client, admin_headers)
    response = await client.get(f"/v1/runs/{run['run_id']}", headers=BOB_SAME_TENANT_APPROVER)
    assert response.status_code == 200


async def test_platform_admin_can_read_any_run(client, admin_headers):
    run = await _make_run(client, admin_headers)
    response = await client.get(f"/v1/runs/{run['run_id']}", headers=admin_headers)
    assert response.status_code == 200


async def test_run_listing_only_returns_own_runs(client, admin_headers):
    await _make_run(client, admin_headers)
    listing = await client.get("/v1/runs", headers=MALLORY)
    assert listing.status_code == 200
    assert listing.json()["items"] == []


async def test_approvals_are_hidden_from_other_tenants(client, admin_headers, approver_headers):
    payload = agent_payload(
        "finance-invoice-auditor",
        endpoint="http://test/agents/finance",
        capabilities=["invoice adjustment"],
        tags=["finance"],
        examples=["Create an adjustment task for invoice INV-9"],
        allowed_roles=["user", "finance"],
        allowed_actions=["read", "write"],
        requires_approval_for=["write"],
        risk_level="high",
    )
    assert (await client.post("/v1/agents", json=payload, headers=admin_headers)).status_code == 201
    started = await client.post(
        "/v1/orchestrate",
        json={"query": "Create an adjustment task for invoice INV-9"},
        headers=ALICE,
    )
    approval_id = started.json()["run"]["approval"]["approval_id"]

    # The requester and a same-tenant approver see it.
    assert (await client.get(f"/v1/approvals/{approval_id}", headers=ALICE)).status_code == 200
    assert (
        await client.get(f"/v1/approvals/{approval_id}", headers=BOB_SAME_TENANT_APPROVER)
    ).status_code == 200

    # Another tenant must not see it, list it, or decide it.
    assert (await client.get(f"/v1/approvals/{approval_id}", headers=MALLORY)).status_code == 403
    assert (
        await client.get(f"/v1/approvals/{approval_id}", headers=CAROL_OTHER_TENANT_APPROVER)
    ).status_code == 403

    listed = await client.get("/v1/approvals", headers=MALLORY)
    assert listed.json()["items"] == []
    listed_other_approver = await client.get("/v1/approvals", headers=CAROL_OTHER_TENANT_APPROVER)
    assert listed_other_approver.json()["items"] == []

    decision = await client.post(
        f"/v1/approvals/{approval_id}/decision",
        json={"approved": True},
        headers=CAROL_OTHER_TENANT_APPROVER,
    )
    assert decision.status_code == 403
