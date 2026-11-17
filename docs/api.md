# API

Base URL: `http://localhost:8080`. Interactive docs: `/docs`.

## Authenticated caller headers

| Header | Required | Meaning |
|---|---|---|
| `X-User-Id` | yes (401 otherwise) | user identity |
| `X-User-Email` | no | display/audit |
| `X-User-Roles` | no | comma/semicolon separated; default `user` |
| `X-User-Team` | no | team |
| `X-Tenant-Id` | no | tenant isolation |
| `X-Service-Id` / `X-Service-Name` | no | calling service |
| `X-Service-Scopes` | no | required by some agents |
| `X-Use-Case` | no | business use case in every trace |
| `X-Trace-Id` | no | 32-hex; joins the UI's trace |
| `X-Workflow-Id` | no | caller's workflow id |

Registry writes additionally require `platform-admin` or `registry-admin`.

## Errors

```json
{
  "error": {
    "code": "policy_denied",
    "message": "Policy denied this agent invocation.",
    "request_id": "req_…"
  }
}
```

| HTTP | Codes |
|---|---|
| 401 | `unauthorized` |
| 403 | `forbidden`, `policy_denied` |
| 404 | `not_found` |
| 409 | `conflict`, `approval_expired`, `approval_required` |
| 422 | `invalid_input` |
| 429 | `budget_exceeded` |
| 501 | `unsupported_protocol` |
| 502/503/504 | `agent_invocation_failed`, `upstream_unavailable`, `agent_timeout` |

---

## POST `/v1/orchestrate`

```json
{
  "query": "Create an adjustment task for invoice INV-2024-0912",
  "environment": "dev",
  "use_case": "invoice_adjustment",
  "conversation": [{"role": "user", "content": "…"}]
}
```

Response (abridged):

```json
{
  "run": {
    "run_id": "run_…",
    "workflow_id": "wf_…",
    "trace_id": "9c1e…",
    "status": "awaiting_approval",
    "response": "This request needs human approval before it can run…",
    "plan": {
      "intent": "…",
      "confidence": 0.83,
      "source": "hybrid",
      "selected_agents": [{"agent_id": "finance-invoice-auditor", "action": "write", "reason": "…"}]
    },
    "agent_results": [],
    "approval": {
      "approval_id": "apv_…",
      "status": "pending",
      "risk_level": "high",
      "reasons": ["… requires approval for 'write'"]
    },
    "policy_decisions": [{"agent_id": "finance-invoice-auditor", "allowed": true, "source": "opa"}],
    "usage": {"calls": 1, "total_tokens": 812, "cost_usd": 0.0004},
    "langfuse_url": null,
    "duration_ms": 143
  }
}
```

`status` is one of `completed`, `awaiting_approval`, `rejected`, `denied`,
`no_match`, `invalid`, `failed`, `timeout`.

---

## POST `/v1/chat/completions`

Standard OpenAI body; `alstom_ao` is added at the top level.

```json
{
  "id": "chatcmpl-…",
  "object": "chat.completion",
  "created": 1767225600,
  "model": "alstom-ao",
  "choices": [{"index": 0, "message": {"role": "assistant", "content": "…"}, "finish_reason": "stop"}],
  "usage": {"prompt_tokens": 120, "completion_tokens": 240, "total_tokens": 360},
  "alstom_ao": {
    "run_id": "run_…",
    "trace_id": "9c1e…",
    "status": "completed",
    "selected_agents": ["hr-policy-advisor"],
    "plan_source": "heuristic",
    "confidence": 0.95,
    "approval": null,
    "policy_denials": []
  }
}
```

`stream: true` returns SSE `chat.completion.chunk` frames ending with `[DONE]`.
The final chunk carries `usage` and `alstom_ao`.

`user` in the body is used only as a fallback identifier when `X-User-Id` is absent.

---

## Approvals

```
GET  /v1/approvals?status=pending
GET  /v1/approvals/{approval_id}
POST /v1/approvals/{approval_id}/decision
     {"approved": true, "note": "checked with vendor"}
```

The decision call records the decision **and resumes the paused graph**, returning
`{"approval": {...}, "run": {...}}`. Approver role required; self-approval refused.

---

## Runs

```
GET /v1/runs?status=awaiting_approval&limit=50
GET /v1/runs/{run_id}
GET /v1/runs/{run_id}/trace
GET /v1/runs/{run_id}/timeline
GET /v1/runs/{run_id}/policy
```

`trace` returns the full ledger; `timeline` returns agent order, latency, retries
and errors.

---

## Registry

```
POST   /v1/agents                 (admin)      → 201 {agent, created}
GET    /v1/agents?environment=dev&status=active&capability=invoice&q=finance
GET    /v1/agents/catalog         (role-filtered planner view)
POST   /v1/agents/discover        (admin)      {"endpoint": "...", "environment": "dev"}
GET    /v1/agents/{id}
PATCH  /v1/agents/{id}            (admin)
DELETE /v1/agents/{id}            (admin)      soft delete → status inactive
POST   /v1/agents/{id}/health     discovery probe → health
```

---

## Policy & ops

```
POST /v1/policies/evaluate   {"agent_id": "...", "action": "query", "environment": "dev"}
GET  /v1/policies/status     OPA reachability + mode
GET  /healthz                liveness
GET  /readyz                 DB ping, adapters, policy, Langfuse, llm mode
GET  /metrics                Prometheus text (counters + latencies)
```

## cURL examples

```bash
H=(-H 'Content-Type: application/json' -H 'X-User-Id: alice' -H 'X-User-Roles: user,finance')

curl -s localhost:8080/v1/orchestrate "${H[@]}" \
  -d '{"query":"Check invoice INV-2024-0912 for duplicate charges"}'

curl -s localhost:8080/v1/agents \
  -H 'X-User-Id: admin' -H 'X-User-Roles: platform-admin'

curl -sN localhost:8080/v1/chat/completions "${H[@]}" \
  -d '{"model":"alstom-ao","stream":true,"messages":[{"role":"user","content":"How many days of annual leave?"}]}'
```
