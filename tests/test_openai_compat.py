"""OpenAI-compatible "virtual model" surface."""

from __future__ import annotations

import json

import pytest

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


@pytest.fixture
async def finance(client, admin_headers):
    response = await client.post("/v1/agents", json=agent_payload(**FINANCE), headers=admin_headers)
    assert response.status_code == 201, response.text


async def test_lists_virtual_model(client):
    response = await client.get("/v1/models")
    assert response.status_code == 200
    ids = [item["id"] for item in response.json()["data"]]
    assert "alstom-ao" in ids


async def test_chat_completion_returns_openai_shape(client, user_headers, finance):
    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "alstom-ao",
            "messages": [{"role": "user", "content": "Check invoice INV-2024-0912 for duplicate charges"}],
        },
        headers=user_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "alstom-ao"
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert "finance handled" in body["choices"][0]["message"]["content"]
    assert body["alstom_ao"]["status"] == "completed"
    assert body["alstom_ao"]["selected_agents"] == ["finance-invoice-auditor"]
    assert body["alstom_ao"]["trace_id"]
    assert set(body["usage"]) == {"prompt_tokens", "completion_tokens", "total_tokens"}


async def test_chat_completion_streams_sse(client, user_headers, finance):
    async with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "alstom-ao",
            "messages": [{"role": "user", "content": "Check invoice INV-2024-0912 for duplicate charges"}],
            "stream": True,
        },
        headers=user_headers,
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        chunks = [chunk async for chunk in response.aiter_text()]

    payloads = [
        frame
        for chunk in chunks
        for frame in chunk.split("\n\n")
        if frame.strip().startswith("data: ")
    ]
    assert payloads[-1].strip() == "data: [DONE]"
    first = json.loads(payloads[0][6:])
    assert first["choices"][0]["delta"]["role"] == "assistant"
    assembled = "".join(
        json.loads(item[6:])["choices"][0]["delta"].get("content", "")
        for item in payloads[:-1]
        if item.strip() != "data: [DONE]"
    )
    assert "finance handled" in assembled


async def test_identity_header_is_required_but_body_user_is_a_fallback(client, finance):
    anonymous = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "Check invoice INV-1"}]},
    )
    assert anonymous.status_code == 401

    fallback = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "Check invoice INV-1"}], "user": "ui-user-7"},
    )
    assert fallback.status_code == 200
    assert fallback.json()["alstom_ao"]["status"] == "completed"


async def test_conversation_history_is_forwarded(client, user_headers, finance, a2a):
    headers = {key: value for key, value in user_headers.items() if key.lower() != "x-use-case"}
    await client.post(
        "/v1/chat/completions",
        json={
            "messages": [
                {"role": "user", "content": "Check invoice INV-1 for duplicate charges"},
                {"role": "assistant", "content": "No duplicate found."},
                {"role": "user", "content": "Check invoice INV-2 for duplicate charges"},
            ]
        },
        headers=headers,
    )
    call = a2a.calls_for("/agents/finance")[0]
    assert "Check invoice INV-2" in call["query"]
    assert call["metadata"]["trace_context"]["use_case"] == "virtual_model"
