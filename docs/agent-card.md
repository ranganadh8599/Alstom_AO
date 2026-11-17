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
| `endpoint` | yes | `http(s)://…`, normalized without trailing slash |
| `owner`, `owner_contact` | no | accountability |
| `capabilities` | yes | ≥1; the routing signal |
| `capability_details` | no | per-capability descriptions/schemas |
| `input_schema`, `output_schema` | no | JSON Schema |
| `auth` | no | `none`/`bearer`/`api_key`/`oauth2` + `secret_ref` |
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
| `data_classification` | `restricted` needs `data-steward`/`platform-admin` |

> A `high` risk card with no `requires_approval_for` automatically gets its
> high-impact allowed actions as approval triggers. Read-only actions never pause.

## Secrets

Never put a secret in a card. Use a reference:

```json
{"auth": {"type": "bearer", "secret_ref": "env:AO_SECRET_FINANCE_TOKEN"}}
```

The orchestrator resolves it at call time and never logs or persists it.

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
    "allowed_roles": ["user", "finance", "approver"],
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
