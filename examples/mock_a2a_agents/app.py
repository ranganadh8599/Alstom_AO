"""Three mock A2A agents used by the local demo and docker-compose.

This is a *reference peer*, not part of the platform: it shows exactly what an
integrated agent has to expose:

* ``GET  /agents/{name}/.well-known/agent-card.json`` - the A2A agent card
* ``POST /agents/{name}`` - JSON-RPC 2.0 ``message/send`` (and legacy ``tasks/send``)

Run with:  uvicorn examples.mock_a2a_agents.app:app --port 9101

The URL advertised in the agent card must be reachable *by the caller*. A gateway
in another container cannot reach ``localhost``, so set ``AO_AGENT_PUBLIC_BASE_URL``
(e.g. ``http://mock-agents:9101`` in compose) rather than hardcoding the host.
"""

from __future__ import annotations

import os
import re
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Mock A2A Agents", version="1.0.0")

PUBLIC_BASE_URL = os.environ.get("AO_AGENT_PUBLIC_BASE_URL", "http://localhost:9101").rstrip("/")

AGENTS: dict[str, dict[str, Any]] = {
    "finance": {
        "name": "Finance Invoice Auditor",
        "description": "Audits vendor invoices, detects duplicates and checks purchase orders.",
        "skills": [
            {
                "id": "invoice-audit",
                "name": "invoice audit",
                "description": "Compare an invoice with its purchase order and payment history.",
                "tags": ["finance", "invoice", "audit", "duplicate"],
                "examples": ["Check invoice INV-2024-0912 for duplicate charges"],
            },
            {
                "id": "payment-status",
                "name": "vendor payment status",
                "description": "Report the payment status of a supplier invoice.",
                "tags": ["finance", "payment", "vendor"],
            },
        ],
    },
    "hr": {
        "name": "HR Policy Advisor",
        "description": "Answers HR policy questions about leave, travel and benefits.",
        "skills": [
            {
                "id": "hr-policy",
                "name": "hr policy lookup",
                "description": "Cite the HR policy that answers a people question.",
                "tags": ["hr", "policy", "leave", "benefits"],
                "examples": ["How many days of annual leave do I get?"],
            }
        ],
    },
    "it": {
        "name": "IT Service Desk",
        "description": "Resolves IT knowledge questions about VPN, access and software.",
        "skills": [
            {
                "id": "vpn-support",
                "name": "vpn troubleshooting",
                "description": "Step-by-step VPN diagnostics.",
                "tags": ["it", "vpn", "access"],
                "examples": ["How do I connect to the VPN from a Mac?"],
            }
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
        "provider": {"organization": "Alstom Demo", "url": PUBLIC_BASE_URL},
        "metadata": {
            "alstom_agent_id": f"demo-{name}-agent",
            "alstom_risk_level": "high" if name == "finance" else ("medium" if name == "it" else "low"),
            "environment": "dev",
            "allowed_roles": ["user", "employee", "finance"],
            "allowed_actions": ["read", "query", "analyze", "write"] if name != "hr" else ["read", "query", "analyze"],
            "requires_approval_for": ["write", "update", "payment"] if name != "hr" else [],
        },
    }


def _answer(name: str, query: str, metadata: dict[str, Any]) -> str:
    invoice = re.search(r"INV[- ]?\d{4}[- ]?\d{3,4}", query, re.IGNORECASE)
    reference = invoice.group(0).upper() if invoice else "the referenced invoice"
    trace = metadata.get("trace_context") or {}
    actor = trace.get("initiated_by", "unknown")
    use_case = trace.get("use_case", "general")

    if name == "finance":
        if "duplicate" in query.lower():
            return (
                f"Invoice {reference}: no duplicate charge detected in the last 24 months. "
                "The line items match purchase order PO-7781 and the contract price list (2024-Q3). "
                "Vendor balance is within tolerance.\n\n"
                f"- Checked by: Finance Invoice Auditor\n- Requested by: {actor}\n- Use case: {use_case}"
            )
        return (
            f"Invoice {reference} is matched to PO-7781 and approved for payment. "
            "One line item is 4% above contract, flagged for a credit note.\n\n"
            f"- Requested by: {actor} · use case: {use_case}"
        )
    if name == "hr":
        return (
            "Under HR policy 4.2 (Leave), employees accrue 25 days of annual leave per calendar year, "
            "plus 5 days after three years of service. Requests over 10 consecutive days need manager approval.\n\n"
            "Source: HR Policy 2024.4, section 4.2.\n"
            f"- Requested by: {actor}"
        )
    return (
        "To connect to the VPN on a Mac: install the Alstom VPN profile from the software catalog, "
        "open Tunnelblick, select `alstom-corp`, and sign in with SSO. If the tunnel drops, "
        "switch to UDP/443 in Preferences > Networking.\n\n"
        "If this still fails, open an IT ticket with the error code.\n"
        f"- Requested by: {actor}"
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
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32601, "message": "unknown agent"}})
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
                "status": {"state": "completed", "message": {"role": "agent", "parts": [{"type": "text", "text": answer}]}},
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
                (metadata.get("trace_context") or {}).get("run_id") or (message.get("messageId") or uuid.uuid4())
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
