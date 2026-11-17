# Trace context and observability

## The envelope

Every agent invocation and every ledger row carries the same context:

```json
{
  "workflow_id": "wf_7f…",
  "trace_id": "9c1e…(32 hex)",
  "run_id": "run_5a…",
  "span_id": "3b9d…",
  "parent_span_id": null,
  "agent_id": "finance-invoice-auditor",
  "agent_name": "Finance Invoice Auditor",
  "agent_version": "2.3.0",
  "protocol": "a2a",
  "action": "write",
  "initiated_by": "alice",
  "authenticated_service": "alstom-ao",
  "use_case": "invoice_adjustment",
  "environment": "dev",
  "timestamp": "2026-01-01T10:00:00Z",
  "status": "succeeded",
  "retry_count": 0,
  "previous_agents": ["hr-policy-advisor"],
  "next_agents": ["it-service-desk"]
}
```

`trace_id` is a W3C/Langfuse-compatible 32-hex id. If the UI sends
`X-Trace-Id`, it is reused so the UI's own traces can join ours; `X-Workflow-Id`
is also honoured.

## Where it goes

| Destination | Content |
|---|---|
| Langfuse | One trace per run; `span` per node, `agent` per invocation; metadata = full context |
| `trace_events` | Append-only ledger row per action with `input`, `output`, latency, retries, error |
| Downstream agent | JSON-RPC `params.metadata.trace_context` + `identity` |
| Downstream headers | `X-Alstom-Trace-Id`, `-Workflow-Id`, `-Run-Id`, `-Agent-Id`, `-Agent-Version`, `-Initiated-By`, `-Service`, `-Use-Case`, `-Action` |
| API response | `run.trace_id`, `run.langfuse_url` |

## Ledger actions

| Action | Meaning |
|---|---|
| `run.started` | Run created, both identities captured |
| `input.validated` | Query accepted (or rejected) |
| `plan.created` / `plan.no_match` | Routing decision, confidence, source (`llm`/`heuristic`/`hybrid`) |
| `policy.evaluated` | Per-agent allow/deny + reasons + source (`opa`/`local-fallback`) |
| `approval.requested` / `approval.decided` | Human gate |
| `agent.invoked` | Request sent (inputs, previous/next agents) |
| `agent.completed` / `agent.failed` | Output, latency, retries |
| `agents.fan_in` | Which agents succeeded/failed |
| `synthesis.completed` | Final answer and failed agents |
| `run.completed` / `run.failed` | Terminal state and usage |

## Answering audit questions

| Question | Query |
|---|---|
| Which agent executed this action? | `GET /v1/runs/{id}/trace` (`agent_id`) |
| Who owns the agent? | registry card `owner` (join by `agent_id`) |
| Which version ran? | `agent_version` |
| Who initiated the flow? | `initiated_by`, `authenticated_service` |
| What were the inputs/outputs? | `input`, `output` |
| Did it fail/retry? | `status`, `error`, `retry_count` |
| Which agents ran before/after? | `trace_context.previous_agents/next_agents`, `GET /timeline` |

## Langfuse

Set `AO_LANGFUSE_ENABLED=true`, `AO_LANGFUSE_PUBLIC_KEY`,
`AO_LANGFUSE_SECRET_KEY`, `AO_LANGFUSE_HOST`. The UI receives a direct link via
`run.langfuse_url`. Tracing failures are swallowed by design: observability must
never break execution.

Every span metadata also includes `user_id` indirectly through
`initiated_by`; filter traces by `metadata.initiated_by` or `metadata.use_case`.
