# Agent identity & capability card

A card is the **mandatory** contract for integrating an agent. No card, no call.

## Lifecycle

1. `POST /v1/agents` (admin) or `POST /v1/agents/discover` (A2A import).
2. `GET /v1/agents/{id}/health` probes the endpoint (discovery document).
3. Planning only considers `status=active`, matching `environment`, and
   non-`unhealthy` cards, filtered by the caller's roles.
4. `DELETE /v1/agents/{id}` deactivates (soft delete) — history and traces remain.

Cards are versioned by `version`; the registry holds one card per `agent_id`.
The planned version is pinned into graph state, so an approval binds to the exact
version that will execute.

## Fields

| Field | Required | Notes |
|---|---|---|
| `agent_id` | yes | `[a-zA-Z0-9._-]`, stable, used in every trace |
| `name` | yes | 2–200 chars |
| `description` | yes | ≥10 chars; used by the planner and heuristic |
| `platform` | no | owning platform (e.g. `alstom-finance`) |
| `protocol` | no | `a2a` (default), `mcp`, `rest` — must have an adapter |
| `endpoint` | yes | `http(s)://…`, normalized without trailing slash. **Not dialled by the orchestrator** — see below |
| `owner`, `owner_contact` | no | accountability |
| `capabilities` | yes | ≥1; the routing signal |
| `capability_details` | no | per-capability descriptions/schemas |
| `input_schema`, `output_schema` | no | JSON Schema |
| `auth` | no | Recorded for the gateway: `none`/`bearer`/`api_key`/`oauth2` + `secret_ref`. **Inert in this service** — see below |
| `permissions` | no | roles, actions, scopes, classification, approval triggers |
| `version` | no | default `1.0.0` |
| `status` | no | `active`/`inactive`/`deprecated` |
| `environment` | no | default `dev`; must match the request environment |
| `risk_level` | no | `low`/`medium`/`high` |
| `tags`, `examples` | no | extra routing signal |
| `metadata` | no | free-form; `tenant_id` is enforced by policy |

## Permissions

| Field | Effect |
|---|---|
| `allowed_roles` | Caller must have one; empty = any authenticated caller |
| `allowed_actions` | Empty = read-only (high-impact actions denied) |
| `high_impact_actions` | Always trigger approval |
| `requires_approval_for` | Always trigger approval for these actions |
| `required_scopes` | Service identity must hold them |
| `data_classification` | `restricted` needs `admin` |

> A `high` risk card with no `requires_approval_for` automatically gets its
> high-impact allowed actions as approval triggers. Read-only actions never pause.

## `endpoint` and `auth` are metadata, not a call path

Agents are reachable **only** through the LiteLLM agent gateway. There is no
direct-agent code path, so neither of these fields is used to make a request:

| Field | Who actually uses it |
|---|---|
| `endpoint` | The gateway's own config. `scripts/export_litellm_agents.py` reads it to generate `agent_card_params.url`; the orchestrator never dials it. |
| `auth` | The gateway. It holds upstream credentials; this service authenticates to the gateway with `AO_LITELLM_PROXY_API_KEY`. |

They remain required/advisory because the registry is the authoritative catalogue
from which gateway config is generated. Do **not** expect editing `auth` to change
how a call authenticates — that lives in `litellm/config.yaml`.

The gateway addresses an agent by name: `card.metadata.litellm_agent_name` when
present, otherwise `card.agent_id`. Set `litellm_agent_name` when the gateway's
registration name differs from the registry id.

## Secrets

Never put a secret in a card. Use a reference:

```json
{"auth": {"type": "bearer", "secret_ref": "env:AO_SECRET_FINANCE_TOKEN"}}
```

In gateway-only mode this reference is a *hint for whoever maintains the gateway
config*: **this service never resolves or sends it.** Keeping secrets out of cards
still matters — cards are stored in Postgres and returned by the API — but the
secret itself belongs in the gateway's environment, not here.

## Example

```json
{
  "agent_id": "finance-invoice-auditor",
  "name": "Finance Invoice Auditor",
  "description": "Audits vendor invoices, detects duplicates and checks purchase orders.",
  "platform": "alstom-finance",
  "protocol": "a2a",
  "endpoint": "http://localhost:9101/agents/finance",
  "owner": "Finance Operations",
  "capabilities": ["invoice audit", "duplicate invoice detection", "vendor payment status"],
  "input_schema": {"type": "object", "properties": {"message": {"type": "string"}}},
  "output_schema": {"type": "object", "properties": {"answer": {"type": "string"}}},
  "auth": {"type": "none"},
  "permissions": {
    "allowed_roles": ["user", "admin"],
    "allowed_actions": ["read", "query", "analyze", "write"],
    "requires_approval_for": ["write", "update", "payment"],
    "data_classification": "confidential"
  },
  "version": "2.3.0",
  "status": "active",
  "environment": "dev",
  "risk_level": "high",
  "tags": ["finance", "invoice", "ap"],
  "examples": ["Check invoice INV-2024-0912 for duplicate charges"]
}
```

See [examples/agent_cards](../examples/agent_cards) for importable files.
