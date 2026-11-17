"""Test fixtures: hermetic SQLite, a fake A2A network, and scripted identities."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from alstom_ao.config import Settings
from alstom_ao.main import create_app, initialize_app, shutdown_app

AGENT_TEXT = "{agent} handled: {query}"


class FakeA2ANetwork:
    """In-memory A2A peer implementing discovery + message/send."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_paths: set[str] = set()
        self.legacy_only = False
        self.empty_paths: set[str] = set()

    def install(self, endpoint: str, *, name: str, version: str = "1.0.0") -> None:
        self._cards[endpoint] = {  # type: ignore[attr-defined]
            "name": name,
            "description": f"{name} test agent",
            "url": endpoint,
            "version": version,
            "capabilities": {"streaming": False},
            "skills": [{"id": name.lower().replace(" ", "-"), "name": name, "tags": ["test"]}],
        }

    _cards: dict[str, dict[str, Any]] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        base = path.split("/.well-known/")[0]
        if path.endswith("/.well-known/agent-card.json") or path.endswith("/.well-known/agent.json"):
            card = self._cards.get(f"http://test{base}")
            if card is None:
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json=card)

        try:
            body = json.loads(request.content or b"{}")
        except ValueError:
            return httpx.Response(400, json={"error": "bad json"})
        agent = path.rstrip("/").split("/")[-1]
        params = body.get("params") or {}
        if self.legacy_only and body.get("method") == "message/send":
            return httpx.Response(404, json={"error": "method not found"})
        query = ""
        message = params.get("message") or {}
        for part in message.get("parts") or []:
            if part.get("kind") == "text" or part.get("type") == "text":
                query = part.get("text", "")
        self.calls.append(
            {
                "path": path,
                "method": body.get("method"),
                "query": query,
                "metadata": params.get("metadata") or {},
                "headers": dict(request.headers),
            }
        )
        if path in self.fail_paths:
            return httpx.Response(503, json={"error": "unavailable"})
        if path in self.empty_paths:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body.get("id"), "result": {"kind": "task", "status": {"state": "completed"}}})
        if body.get("method") == "tasks/send":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body.get("id"),
                    "result": {
                        "id": params.get("id"),
                        "status": {"state": "completed", "message": {"role": "agent", "parts": [{"type": "text", "text": AGENT_TEXT.format(agent=agent, query=query)}]}},
                        "artifacts": [],
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "result": {
                    "kind": "task",
                    "id": params.get("message", {}).get("messageId"),
                    "status": {"state": "completed"},
                    "artifacts": [
                        {
                            "artifactId": "answer",
                            "parts": [{"kind": "text", "text": AGENT_TEXT.format(agent=agent, query=query)}],
                        }
                    ],
                },
            },
        )

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def calls_for(self, path_suffix: str) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["path"].endswith(path_suffix)]


@pytest.fixture
def settings() -> Settings:
    return Settings(
        environment="test",
        database_url="sqlite+aiosqlite:///:memory:",
        db_auto_create=True,
        seed_demo_agents=False,
        require_user_identity=True,
        llm_mode="mock",
        policy_mode="observe",
        opa_url=None,
        policy_local_fallback=True,
        langfuse_enabled=False,
        approval_allow_self=False,
        a2a_timeout_seconds=5,
        a2a_max_retries=1,
        run_timeout_seconds=30,
        max_parallel_agents=4,
        service_id="alstom-ao",
        service_name="agent-orchestrator",
    )


@pytest.fixture
def a2a() -> FakeA2ANetwork:
    FakeA2ANetwork._cards = {}
    network = FakeA2ANetwork()
    for agent in ("finance", "hr", "it"):
        network.install(f"http://test/agents/{agent}", name=f"{agent.upper()} Agent")
    return network


@pytest.fixture
async def app(settings: Settings, a2a: FakeA2ANetwork):
    application = create_app(settings, a2a_transport=a2a.transport)
    await initialize_app(application)
    try:
        yield application
    finally:
        await shutdown_app(application)


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client


@pytest.fixture
def user_headers() -> dict[str, str]:
    return {
        "X-User-Id": "alice",
        "X-User-Email": "alice@example.com",
        "X-User-Roles": "user,finance",
        "X-User-Team": "finance",
        "X-Tenant-Id": "tenant-a",
        "X-Use-Case": "test",
    }


@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {
        "X-User-Id": "admin",
        "X-User-Roles": "platform-admin",
        "X-Tenant-Id": "tenant-a",
    }


@pytest.fixture
def approver_headers() -> dict[str, str]:
    return {
        "X-User-Id": "approver-bob",
        "X-User-Roles": "approver",
        "X-Tenant-Id": "tenant-a",
    }


def agent_payload(
    agent_id: str = "test-agent",
    *,
    endpoint: str = "http://test/agents/test",
    environment: str = "test",
    risk_level: str = "low",
    allowed_roles: list[str] | None = None,
    allowed_actions: list[str] | None = None,
    requires_approval_for: list[str] | None = None,
    required_scopes: list[str] | None = None,
    capabilities: list[str] | None = None,
    tags: list[str] | None = None,
    examples: list[str] | None = None,
    version: str = "1.0.0",
    auth: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "agent_id": agent_id,
        "name": f"{agent_id} display",
        "description": f"Test agent {agent_id} that answers domain questions with citations and checks.",
        "platform": "test",
        "protocol": "a2a",
        "endpoint": endpoint,
        "owner": "qa",
        "capabilities": capabilities or ["invoice audit", "duplicate invoice detection"],
        "input_schema": {"type": "object", "properties": {"message": {"type": "string"}}},
        "output_schema": {"type": "object", "properties": {"answer": {"type": "string"}}},
        "auth": auth or {"type": "none"},
        "permissions": {
            "allowed_roles": allowed_roles or ["user", "finance"],
            "allowed_actions": allowed_actions or ["read", "query", "analyze", "write"],
            "requires_approval_for": requires_approval_for or [],
            "required_scopes": required_scopes or [],
            "high_impact_actions": [],
            "data_classification": "internal",
        },
        "version": version,
        "status": "active",
        "environment": environment,
        "risk_level": risk_level,
        "tags": tags or ["finance", "invoice"],
        "examples": examples or ["Check invoice INV-1 for duplicate charges"],
    }
