"""LiteLLM agent-gateway routing: JSON-RPC through POST /a2a/{agent}."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from alstom_ao.adapters.a2a import A2AAdapter
from alstom_ao.config import Settings
from alstom_ao.domain.enums import ActionType, TraceStatus
from alstom_ao.domain.models import (
    AgentCard,
    IdentityContext,
    InvocationRequest,
    ServiceIdentity,
    TraceContext,
    UserIdentity,
)
from alstom_ao.errors import AgentInvocationError
from alstom_ao.main import create_app, initialize_app, shutdown_app
from tests.conftest import agent_payload

PROXY = "http://proxy:4000"
KEY = "sk-test-virtual-key"


class FakeLiteLLMGateway:
    """Mimics the LiteLLM agent gateway surface used by the adapter."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.cards: dict[str, dict[str, Any]] = {}
        self.fail_status: int | None = None

    def register(self, name: str, *, version: str = "1.0.0") -> None:
        self.cards[name] = {
            "name": name,
            "description": f"{name} via LiteLLM",
            "url": f"{PROXY}/a2a/{name}",
            "version": version,
            "protocolVersion": "0.3",
            "capabilities": {"streaming": False},
            "skills": [{"id": name, "name": name, "tags": ["test"]}],
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        headers = {key.lower(): value for key, value in request.headers.items()}
        self.calls.append({"path": path, "method": request.method, "headers": headers})

        if request.method == "GET" and path.endswith("/.well-known/agent.json"):
            name = path.strip("/").split("/")[-3]
            card = self.cards.get(name)
            if card is None:
                return httpx.Response(404, json={"error": "unknown agent"})
            return httpx.Response(200, json=card)

        if request.method == "GET":
            return httpx.Response(404, json={"error": "not found"})

        if self.fail_status is not None:
            return httpx.Response(self.fail_status, json={"error": "gateway error"})

        body = json.loads(request.content or b"{}")
        params = body.get("params") or {}
        text = ""
        for part in (params.get("message") or {}).get("parts") or []:
            text = part.get("text", "") or text
        name = path.strip("/").split("/")[-1]
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "result": {
                    "kind": "task",
                    "id": (params.get("message") or {}).get("messageId"),
                    "status": {"state": "completed"},
                    "artifacts": [
                        {"artifactId": "answer", "parts": [{"kind": "text", "text": f"{name} via gateway: {text}"}]}
                    ],
                    "metadata": {"gateway": "litellm", "upstream": name},
                },
            },
        )

    def invocations(self) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["method"] == "POST"]


def proxy_settings(**overrides) -> Settings:
    base = dict(
        environment="test",
        database_url="sqlite+aiosqlite:///:memory:",
        opa_url=None,
        llm_mode="mock",
        policy_mode="observe",
        seed_demo_agents=False,
        litellm_proxy_url=PROXY,
        litellm_proxy_api_key=KEY,
        a2a_max_retries=0,
        a2a_timeout_seconds=3,
    )
    base.update(overrides)
    return Settings(**base)


def make_card(**overrides) -> AgentCard:
    payload = agent_payload("finance-invoice-auditor", endpoint="http://agent-direct:9101/agents/finance")
    payload.update(overrides)
    return AgentCard(**payload)


def make_request(agent_id: str = "finance-invoice-auditor") -> InvocationRequest:
    identity = IdentityContext(
        user=UserIdentity(user_id="alice", roles=["user"], tenant_id="tenant-a"),
        service=ServiceIdentity(service_id="alstom-ao"),
    )
    trace = TraceContext(
        workflow_id="wf_proxy",
        trace_id="b" * 32,
        run_id="run_proxy",
        agent_id=agent_id,
        agent_version="1.0.0",
        protocol="a2a",
        action=ActionType.QUERY,
        initiated_by="alice",
        authenticated_service="alstom-ao",
        use_case="proxy_test",
    )
    return InvocationRequest(query="check invoice INV-1", trace=trace, identity=identity)


async def test_proxy_mode_posts_jsonrpc_to_gateway():
    gateway = FakeLiteLLMGateway()
    adapter = A2AAdapter(proxy_settings(), transport=httpx.MockTransport(gateway))
    result = await adapter.invoke(make_card(), make_request())
    await adapter.aclose()

    assert result.status == TraceStatus.SUCCEEDED
    assert "via gateway" in result.text

    call = gateway.invocations()[0]
    assert call["path"] == "/a2a/finance"
    assert call["headers"]["x-litellm-api-key"] == f"Bearer {KEY}"
    assert call["headers"]["a2a-version"] == "0.3"
    assert call["headers"]["x-litellm-trace-id"] == "b" * 32
    assert call["headers"]["x-litellm-agent-id"] == "finance"
    # Our own trace headers still travel, so the gateway can forward them.
    assert call["headers"]["x-alstom-trace-id"] == "b" * 32


async def test_proxy_mode_uses_litellm_agent_name_from_metadata():
    gateway = FakeLiteLLMGateway()
    adapter = A2AAdapter(proxy_settings(), transport=httpx.MockTransport(gateway))
    card = make_card(metadata={"litellm_agent_name": "ij-local"})
    await adapter.invoke(card, make_request())
    await adapter.aclose()

    assert gateway.invocations()[0]["path"] == "/a2a/ij-local"
    assert gateway.invocations()[0]["headers"]["x-litellm-agent-id"] == "ij-local"


async def test_proxy_mode_discovers_agent_card_through_gateway():
    gateway = FakeLiteLLMGateway()
    gateway.register("finance-invoice-auditor")
    adapter = A2AAdapter(proxy_settings(), transport=httpx.MockTransport(gateway))
    document = await adapter.discover("finance-invoice-auditor")
    await adapter.aclose()

    assert document is not None
    assert document["name"] == "finance-invoice-auditor"
    # Standard path first, legacy fallback second - both through the gateway.
    assert gateway.calls[0]["path"] == "/a2a/finance-invoice-auditor/.well-known/agent-card.json"
    assert gateway.calls[1]["path"] == "/a2a/finance-invoice-auditor/.well-known/agent.json"


async def test_proxy_discovery_accepts_a_full_agent_url():
    gateway = FakeLiteLLMGateway()
    gateway.register("it-service-desk")
    adapter = A2AAdapter(proxy_settings(), transport=httpx.MockTransport(gateway))
    document = await adapter.discover("http://legacy-host:9101/agents/it-service-desk")
    await adapter.aclose()

    assert document is not None
    assert gateway.calls[0]["path"] == "/a2a/it-service-desk/.well-known/agent-card.json"
    assert gateway.calls[1]["path"] == "/a2a/it-service-desk/.well-known/agent.json"


async def test_proxy_mode_requires_a_virtual_key():
    adapter = A2AAdapter(
        proxy_settings(litellm_proxy_api_key=None, litellm_proxy_api_key_ref=None),
        transport=httpx.MockTransport(FakeLiteLLMGateway()),
    )
    with pytest.raises(AgentInvocationError) as exc:
        await adapter.invoke(make_card(), make_request())
    assert exc.value.code == "missing_proxy_key"
    await adapter.aclose()


async def test_proxy_mode_does_not_fall_back_to_legacy_tasks_send():
    gateway = FakeLiteLLMGateway()
    gateway.fail_status = 404
    adapter = A2AAdapter(proxy_settings(), transport=httpx.MockTransport(gateway))
    with pytest.raises(AgentInvocationError):
        await adapter.invoke(make_card(), make_request())
    await adapter.aclose()

    assert len(gateway.invocations()) == 1


async def test_proxy_mode_can_request_wire_format_1_0():
    gateway = FakeLiteLLMGateway()
    adapter = A2AAdapter(proxy_settings(litellm_a2a_protocol_version="1.0"), transport=httpx.MockTransport(gateway))
    await adapter.invoke(make_card(), make_request())
    await adapter.aclose()
    assert gateway.invocations()[0]["headers"]["a2a-version"] == "1.0"


async def test_adapter_never_dials_the_agent_endpoint():
    """The card's endpoint is metadata only: every call goes to the gateway."""

    gateway = FakeLiteLLMGateway()
    adapter = A2AAdapter(proxy_settings(), transport=httpx.MockTransport(gateway))
    await adapter.invoke(make_card(endpoint="http://agent-direct.invalid/agents/finance"), make_request())
    await adapter.aclose()

    calls = gateway.invocations()
    assert len(calls) == 1
    assert calls[0]["path"] == "/a2a/finance"
    assert "agent-direct.invalid" not in str(calls[0]["path"])


# ---------------------------------------------------------------------------
# end-to-end through the orchestration API
# ---------------------------------------------------------------------------


@pytest.fixture
async def proxy_app(proxy_settings_fixture, gateway: FakeLiteLLMGateway):
    application = create_app(proxy_settings_fixture, a2a_transport=httpx.MockTransport(gateway))
    await initialize_app(application)
    try:
        yield application
    finally:
        await shutdown_app(application)


@pytest.fixture
def proxy_settings_fixture() -> Settings:
    return proxy_settings()


@pytest.fixture
def gateway() -> FakeLiteLLMGateway:
    gateway = FakeLiteLLMGateway()
    gateway.register("finance-invoice-auditor")
    return gateway


@pytest.fixture
async def proxy_client(proxy_app):
    transport = httpx.ASGITransport(app=proxy_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_orchestration_routes_agent_calls_through_the_gateway(
    proxy_client, admin_headers, user_headers, gateway: FakeLiteLLMGateway
):
    payload = agent_payload(
        "finance-invoice-auditor",
        endpoint="http://agent-direct:9101/agents/finance",
        capabilities=["invoice audit", "duplicate invoice detection"],
        tags=["finance", "invoice"],
        examples=["Check invoice INV-2024-0912 for duplicate charges"],
        allowed_roles=["admin"],
        allowed_actions=["read", "query", "analyze"],
    )
    created = await proxy_client.post("/v1/agents", json=payload, headers=admin_headers)
    assert created.status_code == 201, created.text

    response = await proxy_client.post(
        "/v1/orchestrate",
        json={"query": "Check invoice INV-2024-0912 for duplicate charges"},
        headers=user_headers,
    )
    assert response.status_code == 200, response.text
    run = response.json()["run"]
    assert run["status"] == "completed"
    assert "via gateway" in run["response"]

    agent_calls = gateway.invocations()
    assert len(agent_calls) == 1
    assert agent_calls[0]["path"] == "/a2a/finance"
    assert agent_calls[0]["headers"]["x-litellm-trace-id"] == run["trace_id"]


async def test_health_probe_uses_the_gateway_in_proxy_mode(
    proxy_client, admin_headers, gateway: FakeLiteLLMGateway
):
    # The card's endpoint yields litellm_agent_name "finance", so that is the
    # gateway name the probe must use.
    gateway.register("finance")
    payload = agent_payload("finance-invoice-auditor", endpoint="http://agent-direct:9101/agents/finance")
    await proxy_client.post("/v1/agents", json=payload, headers=admin_headers)

    health = await proxy_client.post("/v1/agents/finance-invoice-auditor/health", headers=admin_headers)
    assert health.status_code == 200
    assert health.json()["health"] == "healthy"
    assert any(call["path"].endswith("/.well-known/agent.json") for call in gateway.calls)

# ---------------------------------------------------------------------------
# gateway-only policy for inter-agent traffic
# ---------------------------------------------------------------------------


def test_there_is_no_routing_switch():
    """Direct agent invocation was removed from the codebase entirely."""

    from alstom_ao.config import Settings

    assert not hasattr(Settings(environment="dev"), "a2a_routing")


def test_gateway_url_is_mandatory():
    from alstom_ao.config import Settings

    with pytest.raises(ValueError) as exc:
        Settings(environment="dev", litellm_proxy_url="")
    assert "AO_LITELLM_PROXY_URL" in str(exc.value)


def test_gateway_url_must_not_be_blank():
    from alstom_ao.config import Settings

    with pytest.raises(ValueError):
        Settings(environment="prod", litellm_proxy_url="   ")


async def test_readyz_reports_the_agent_gateway(settings, a2a):
    """Readiness must not claim "ok" when agent traffic has no gateway."""

    from alstom_ao.main import create_app, initialize_app, shutdown_app

    gateway_down = settings.model_copy(update={"litellm_proxy_url": "http://127.0.0.1:1"})
    app = create_app(gateway_down, a2a_transport=a2a.transport)
    await initialize_app(app)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/readyz")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "degraded"
        assert body["checks"]["agent_gateway"].startswith("unreachable")
    finally:
        await shutdown_app(app)
