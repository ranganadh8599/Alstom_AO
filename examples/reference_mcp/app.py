"""Reference MCP server used to exercise the MCP adapter locally.

Not part of the platform. It exposes three read-only tools over MCP streamable
HTTP so a fresh checkout has something to route to before real MCP servers are
registered.

Why a separate process and not an agent: an MCP server publishes **tools**, not
conversational agents. The orchestrator therefore treats one card as one tool on
one server, and calls it through the LiteLLM MCP gateway
(``POST {proxy}/mcp-rest/tools/call``) rather than speaking MCP itself.

Run directly:

    python examples/reference_mcp/app.py

Or via compose, where it is the ``mcp-reference`` service on port 9102. The MCP
endpoint is ``http://mcp-reference:9102/mcp``.
"""

from __future__ import annotations

import os
import re

from mcp.server.fastmcp import FastMCP

INVOICES: dict[str, dict[str, str]] = {
    "INV-2024-0912": {
        "vendor": "Nordwind Components",
        "purchase_order": "PO-7781",
        "amount": "48,320.00 EUR",
        "status": "approved for payment",
        "note": "One line item is 4% above contract, flagged for a credit note.",
    },
    "INV-2025-0031": {
        "vendor": "Harbour Logistics",
        "purchase_order": "PO-8104",
        "amount": "12,900.00 EUR",
        "status": "on hold",
        "note": "Missing goods-receipt confirmation.",
    },
}

PURCHASE_ORDERS: dict[str, dict[str, str]] = {
    "PO-7781": {
        "vendor": "Nordwind Components",
        "contract": "2024-Q3 price list",
        "amount": "46,400.00 EUR",
        "open": "true",
    },
    "PO-8104": {
        "vendor": "Harbour Logistics",
        "contract": "2024-Q4 price list",
        "amount": "12,900.00 EUR",
        "open": "false",
    },
}


def _normalise(value: str) -> str:
    return (value or "").strip().upper()


def _invoice_key(value: str) -> str:
    """Accept a clean id or free text; pull the invoice reference out of either.

    Real MCP tools are often called with whatever the planner produced, which may
    be a sentence rather than an id. Being tolerant here keeps the adapter demo
    honest instead of relying on a pinned argument.
    """

    match = re.search(r"INV[- ]?\d{4}[- ]?\d{3,4}", value or "", re.IGNORECASE)
    if match:
        return match.group(0).upper().replace(" ", "-")
    return _normalise(value)


def _po_key(value: str) -> str:
    """Accept a clean PO number or free text; pull the PO reference out of either."""

    match = re.search(r"PO[- ]?\d{3,6}", value or "", re.IGNORECASE)
    if match:
        return match.group(0).upper().replace(" ", "-")
    return _normalise(value)


mcp = FastMCP("alstom-reference-mcp")


@mcp.tool()
def invoice_status(invoice_id: str) -> str:
    """Report the payment status and matching purchase order of a vendor invoice."""

    key = _invoice_key(invoice_id)
    record = INVOICES.get(key)
    if not record:
        return f"Invoice {key or '(none)'} was not found in the invoice register."
    return (
        f"Invoice {key}\n"
        f"- Vendor: {record['vendor']}\n"
        f"- Purchase order: {record['purchase_order']}\n"
        f"- Amount: {record['amount']}\n"
        f"- Status: {record['status']}\n"
        f"- Note: {record['note']}"
    )


@mcp.tool()
def purchase_order_lookup(po_number: str) -> str:
    """Look up a purchase order: vendor, contract, amount and whether it is still open."""

    key = _po_key(po_number)
    record = PURCHASE_ORDERS.get(key)
    if not record:
        return f"Purchase order {key or '(none)'} was not found."
    return (
        f"Purchase order {key}\n"
        f"- Vendor: {record['vendor']}\n"
        f"- Contract: {record['contract']}\n"
        f"- Amount: {record['amount']}\n"
        f"- Open: {record['open']}"
    )


@mcp.tool()
def extract_invoice_reference(text: str) -> str:
    """Extract the first invoice reference (INV-...) from free text."""

    match = re.search(r"INV[- ]?\d{4}[- ]?\d{3,4}", text or "", re.IGNORECASE)
    return match.group(0).upper() if match else ""


def main() -> None:
    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = int(os.environ.get("MCP_PORT", "9102"))
    # The gateway dials us as http://mcp-reference:9102/mcp, so the container
    # hostname must be an allowed Host header. The SDK defaults to localhost only
    # and answers 421 Misdirected Request otherwise.
    public_host = os.environ.get("MCP_PUBLIC_HOST", "mcp-reference")
    security = mcp.settings.transport_security
    security.allowed_hosts = [*security.allowed_hosts, f"{public_host}:*"]
    security.allowed_origins = [*security.allowed_origins, f"http://{public_host}:*"]
    # streamable-http serves JSON-RPC on /mcp, which is the URL LiteLLM dials.
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
