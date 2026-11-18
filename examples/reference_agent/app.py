"""Reference A2A agent used to exercise the full orchestration path locally.

This is **not** part of the platform. It exists so a fresh checkout has exactly one
honest, reproducible agent to test against before real agents are registered. It
implements the two endpoints an integrated agent must expose:

    GET  /agents/{name}/.well-known/agent-card.json   the A2A agent card
    POST /agents/{name}                               JSON-RPC 2.0 message/send

Run it directly:

    uvicorn examples.reference_agent.app:app --port 9101

Or via the compose stack, where it is the `reference-agent` service.

The URL advertised in the card must be reachable *by the caller*. A gateway in
another container cannot reach ``localhost``, so set ``AO_AGENT_PUBLIC_BASE_URL``
(e.g. ``http://reference-agent:9101`` in compose) rather than hardcoding the host.
"""

from __future__ import annotations

import os
import re
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Reference A2A Agent", version="1.0.0")

PUBLIC_BASE_URL = os.environ.get("AO_AGENT_PUBLIC_BASE_URL", "http://localhost:9101").rstrip("/")

# One agent, deliberately. It covers read *and* write so the approval path is
# testable without registering anything else.
AGENTS: dict[str, dict[str, Any]] = {
    "finance": {
        "name": "Finance Invoice Auditor",
        "description": "Audits vendor invoices, detects duplicate charges and checks purchase orders.",
        "skills": [
            {
                "id": "invoice-audit",
                "name": "invoice audit",
                "description": "Compare an invoice with its purchase order and payment history.",
                "tags": ["finance", "invoice", "audit", "duplicate"],
                "examples": ["Check invoice INV-2024-0912 for duplicate charges"],
            },
        ],
    },
}


def _card(name: str) -> dict[str, Any]:
    agent = AGENTS[name]
    url = f"{PUBLIC_BASE_URL}/agents/{name}"
    return {
        # A2A 0.3: a2a-sdk 1.x (used by the LiteLLM gateway) refuses to build a
        # client without an explicit transport binding, so the card must declare
        # both `preferredTransport` and the interface list.
        "protocolVersion": "0.3",
        "preferredTransport": "JSONRPC",
        "additionalInterfaces": [{"url": url, "transport": "JSONRPC"}],
        "name": agent["name"],
        "description": agent["description"],
        "url": url,
        "version": "1.0.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "skills": agent["skills"],
        "provider": {"organization": "Reference", "url": PUBLIC_BASE_URL},
        "metadata": {
            "alstom_agent_id": "demo-finance-agent",
            "alstom_risk_level": "high",
            "environment": "dev",
            "allowed_roles": ["user", "admin"],
            "allowed_actions": ["read", "query", "analyze", "write"],
            "requires_approval_for": ["write", "update", "payment"],
        },
    }


def _answer(name: str, query: str, metadata: dict[str, Any]) -> str:
    invoice = re.search(r"INV[- ]?\d{4}[- ]?\d{3,4}", query, re.IGNORECASE)
    reference = invoice.group(0).upper() if invoice else "the referenced invoice"
    trace = metadata.get("trace_context") or {}
    actor = trace.get("initiated_by", "unknown")
    use_case = trace.get("use_case", "general")

    if "duplicate" in query.lower():
        return (
            f"Invoice {reference}: no duplicate charge detected in the last 24 months. "
            "Line items match purchase order PO-7781 and the 2024-Q3 contract price list. "
            "Vendor balance is within tolerance.\n\n"
            f"- Checked by: Finance Invoice Auditor\n- Requested by: {actor}\n- Use case: {use_case}"
        )
    return (
        f"Invoice {reference} is matched to PO-7781 and approved for payment. "
        "One line item is 4% above contract, flagged for a credit note.\n\n"
        f"- Requested by: {actor} - use case: {use_case}"
    )


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "agents": sorted(AGENTS)}


@app.get("/agents/{name}/.well-known/agent-card.json")
async def agent_card(name: str):
    if name not in AGENTS:
        return JSONResponse({"error": "unknown agent"}, status_code=404)
    return _card(name)


@app.post("/agents/{name}")
async def rpc(name: str, request: Request):
    if name not in AGENTS:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32601, "message": "unknown agent"}}
        )

    body = await request.json()
    method = body.get("method")
    params = body.get("params") or {}
    metadata = params.get("metadata") or {}
    message = params.get("message") or {}
    query = " ".join(
        part.get("text", "") for part in message.get("parts") or [] if isinstance(part, dict)
    ).strip()
    if not query:
        query = str(params.get("id") or "")

    answer = _answer(name, query, metadata)
    rpc_id = body.get("id")

    if method == "tasks/send":
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "id": params.get("id") or str(uuid.uuid4()),
                "status": {
                    "state": "completed",
                    "message": {"role": "agent", "parts": [{"type": "text", "text": answer}]},
                },
                "artifacts": [],
            },
        }

    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "result": {
            "kind": "task",
            "id": message.get("messageId") or str(uuid.uuid4()),
            # a2a-sdk 1.x validates contextId as a required string; never return null.
            "contextId": str(
                (metadata.get("trace_context") or {}).get("run_id")
                or message.get("messageId")
                or uuid.uuid4()
            ),
            "status": {"state": "completed"},
            "artifacts": [
                {
                    "artifactId": f"{name}-answer",
                    "name": "answer",
                    "parts": [{"kind": "text", "text": answer}],
                }
            ],
            "metadata": {
                "agent": name,
                "trace_id": metadata.get("trace_context", {}).get("trace_id"),
            },
        },
    }
