# Policy

## Decision point

Before any agent is called, for every selected agent:

```
POST {AO_OPA_URL}/v1/data/alstom/authz/decision
{
  "input": {
    "user":    {"user_id": "...", "roles": [], "team": "...", "tenant_id": "..."},
    "service": {"service_id": "alstom-ao", "scopes": []},
    "agent":   {"agent_id": "...", "status": "active", "environment": "dev",
                "risk_level": "high", "permissions": {...}, "metadata": {...}},
    "action": "write",
    "environment": "dev",
    "request": {"use_case": "...", "query": "..."}
  }
}
```

Response:

```json
{"result": {"allow": true, "reasons": [], "obligations": {"requires_approval": true}}}
```

## Rules (`policies/agent_authz.rego`)

| Check | Denial reason |
|---|---|
| Agent `active` | `agent_not_active` |
| Agent environment matches request | `environment_mismatch` |
| Caller role ∈ `allowed_roles` | `role_not_permitted` |
| Action ∈ `allowed_actions` (empty = read-only) | `action_not_permitted` |
| Service holds `required_scopes` | `service_scope_missing` |
| Tenant matches `metadata.tenant_id` | `tenant_mismatch` |
| `restricted` needs `admin` | `restricted_data_role_missing` |

`admin` bypasses environment/role/action/scope/tenant checks (status is always
required). `requires_approval` is emitted for high-risk + high-impact actions and
for `requires_approval_for` actions.

A tenant-scoped agent is denied unless the caller's tenant matches **exactly**.
A caller with no tenant does not inherit access.

## Modes

| Mode | OPA reachable | OPA unreachable |
|---|---|---|
| `observe` | denial recorded, execution continues (`denied_in_observe_mode`) | local fallback, then continue |
| `enforce` | denial blocks that agent | local fallback; if disabled → fail closed |

`AO_POLICY_LOCAL_FALLBACK=false` plus `AO_POLICY_MODE=enforce` is the strict
production posture. Start in `observe` to build confidence, then flip.

## Local parity

`local_decision()` implements the same rules in Python for air-gapped or
OPA-outage operation. If you add a Rego rule, mirror it there (or set
`AO_POLICY_LOCAL_FALLBACK=false` and accept fail-closed behavior).

The fallback must fail closed wherever the Rego does. In particular, a missing
caller tenant never grants access to a tenant-scoped agent.

The two implementations are not automatically checked against each other — if you
change a rule, update `local_decision()` in the same change.

## Approval authorization (`policies/approval.rego`)

Who may decide an approval:

- must hold `admin`;
- must not be the requester (separation of duties).

The decision point defaults to `alstom/approval/decision` and is configurable
via `AO_OPA_APPROVAL_DECISION_PATH`.

## Running OPA

```bash
opa run --server --addr=0.0.0.0:8181 ./policies
```

Docker Compose mounts `./policies` read-only. Validate before serving:

```bash
opa check ./policies
```

Test a rule quickly:

```bash
curl -s localhost:8181/v1/data/alstom/authz/decision \
  -H 'Content-Type: application/json' \
  -d '{"input":{"agent":{"status":"active","environment":"dev","risk_level":"high","permissions":{"allowed_roles":["user"],"allowed_actions":["query"]}},"user":{"roles":["user"]},"service":{"scopes":[]},"action":"query","environment":"dev","request":{}}}'
```

## Audit

Every decision is written to `policy_decisions` and emitted as
`policy.evaluated` in the trace (`allowed`, `reasons`, `source`, `obligations`).
`GET /v1/runs/{id}/policy` returns them; `POST /v1/policies/evaluate` is a dry run.
