"""A2A adapter: discovery, invocation, legacy fallback, auth and error mapping."""

from __future__ import annotations

import httpx
import pytest

from alstom_ao.adapters.a2a import A2AAdapter, parse_a2a_result
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
from tests.conftest import FakeA2ANetwork, agent_payload


def settings(**overrides) -> Settings:
    base = dict(
        environment="test",
        database_url="sqlite+aiosqlite:///:memory:",
        opa_url=None,
        # These tests exercise the adapter against fake peers directly.
        # Production routing is gateway-only and covered in test_a2a_proxy.py.
        a2a_routing="direct",
        a2a_timeout_seconds=2,
        a2a_max_retries=0,
    )
    base.update(overrides)
    return Settings(**base)


def make_card(**overrides) -> AgentCard:
    return AgentCard(**agent_payload("adapter-agent", endpoint="http://test/agents/finance", **overrides))


def make_request(agent_id: str = "adapter-agent") -> InvocationRequest:
    identity = IdentityContext(
        user=UserIdentity(user_id="alice", roles=["user"], tenant_id="tenant-a"),
        service=ServiceIdentity(service_id="alstom-ao"),
    )
    trace = TraceContext(
        workflow_id="wf_1",
        trace_id="a" * 32,
        run_id="run_1",
        agent_id=agent_id,
        agent_version="1.0.0",
        protocol="a2a",
        action=ActionType.QUERY,
        initiated_by="alice",
        authenticated_service="alstom-ao",
        use_case="test",
        previous_agents=["first-agent"],
        next_agents=["third-agent"],
    )
    return InvocationRequest(query="check invoice INV-1", trace=trace, identity=identity, instructions="be brief")


async def test_discovers_agent_card(a2a: FakeA2ANetwork):
    adapter = A2AAdapter(settings(), transport=a2a.transport)
    document = await adapter.discover("http://test/agents/finance")
    assert document is not None
    assert document["name"] == "FINANCE Agent"


async def test_invokes_message_send_and_propagates_context(a2a: FakeA2ANetwork):
    adapter = A2AAdapter(settings(), transport=a2a.transport)
    result = await adapter.invoke(make_card(), make_request())
    await adapter.aclose()

    assert result.status == TraceStatus.SUCCEEDED
    assert "finance handled" in result.text
    assert result.agent_version == "1.0.0"
    call = a2a.calls_for("/agents/finance")[0]
    assert call["method"] == "message/send"
    assert "Focus: be brief" in call["query"]
    assert call["metadata"]["trace_context"]["previous_agents"] == ["first-agent"]
    assert call["metadata"]["agent_identity"]["agent_id"] == "adapter-agent"
    assert call["headers"]["x-alstom-run-id"] == "run_1"


async def test_falls_back_to_legacy_tasks_send(a2a: FakeA2ANetwork):
    a2a.legacy_only = True
    adapter = A2AAdapter(settings(), transport=a2a.transport)
    result = await adapter.invoke(make_card(), make_request())
    await adapter.aclose()

    assert result.status == TraceStatus.SUCCEEDED
    methods = [call["method"] for call in a2a.calls_for("/agents/finance")]
    assert methods == ["tasks/send"]


async def test_failed_agent_state_is_mapped(a2a: FakeA2ANetwork):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "result": {"kind": "task", "status": {"state": "failed", "message": {"parts": [{"kind": "text", "text": "boom"}]}}},
            },
        )

    adapter = A2AAdapter(settings(), transport=httpx.MockTransport(handler))
    result = await adapter.invoke(make_card(), make_request())
    await adapter.aclose()

    assert result.status == TraceStatus.FAILED
    assert "boom" in (result.error or "")


async def test_jsonrpc_error_raises(a2a: FakeA2ANetwork):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": "1", "error": {"code": -32601, "message": "nope"}})

    adapter = A2AAdapter(settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(AgentInvocationError):
        await adapter.invoke(make_card(), make_request())
    await adapter.aclose()


async def test_missing_bearer_secret_is_reported(a2a: FakeA2ANetwork, monkeypatch):
    monkeypatch.delenv("AO_TEST_TOKEN", raising=False)
    card = make_card(auth={"type": "bearer", "secret_ref": "env:AO_TEST_TOKEN"})
    adapter = A2AAdapter(settings(), transport=a2a.transport)
    with pytest.raises(AgentInvocationError) as exc:
        await adapter.invoke(card, make_request())
    assert "not resolvable" in str(exc.value)
    await adapter.aclose()


def test_parse_a2a_result_merges_status_and_artifacts():
    text, artifacts, state, error = parse_a2a_result(
        {
            "kind": "task",
            "status": {"state": "completed", "message": {"parts": [{"kind": "text", "text": "status text"}]}},
            "artifacts": [{"artifactId": "a", "parts": [{"kind": "text", "text": "artifact text"}]}],
        }
    )
    assert state == "completed"
    assert error is None
    assert "status text" in text
    assert "artifact text" in text
    assert len(artifacts) == 1


async def test_unreachable_agent_raises_after_retries(a2a: FakeA2ANetwork):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope", request=request)

    adapter = A2AAdapter(settings(a2a_max_retries=1), transport=httpx.MockTransport(handler))
    with pytest.raises(AgentInvocationError):
        await adapter.invoke(make_card(), make_request())
    await adapter.aclose()
