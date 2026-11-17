# LiteLLM agent gateway (A2A)

The orchestration layer can reach agents in two ways. The wire protocol is
identical in both; only the **address and credentials** change.

| Mode | `AO_A2A_ROUTING` | JSON-RPC target | Auth we send |
|---|---|---|---|
| Direct | `direct` (default) | `card.endpoint` | the agent's own `auth` block (`env:` secret) |
| LiteLLM gateway | `litellm_proxy` | `{AO_LITELLM_PROXY_URL}/a2a/{agent_name}` | LiteLLM virtual key |

```mermaid
flowchart LR
    O["Orchestrator<br/>(registry, policy, approvals, tracing)"] -->|"POST {proxy}/a2a/{agent}<br/>JSON-RPC message/send"| L["LiteLLM agent gateway"]
    L -->|routes, logs, budgets,<br/>version conversion| A1["Finance agent"]
    L --> A2["HR agent"]
    L --> A3["IT agent"]
    O -.->|"direct mode instead"| A1
```

The gateway *can* enforce its own budgets, but this deployment deliberately leaves
them unset — see [Budgets across the two layers](#budgets-across-the-two-layers).

## Why route through the gateway

- Central request/response logs and per-agent spend for **agent** traffic.
- Load balancing and provider fallbacks across agents.
- Protocol normalisation: the proxy can serve A2A `0.3` or `1.0` wire format to us
  while the upstream agent speaks either.
- One place to hold upstream agent credentials; our cards then only need the
  gateway name.

What does **not** move to the gateway: the registry stays the source of truth for
capability, ownership, permissions and risk; policy, approvals and tracing stay
in the orchestration layer. The gateway executes, we govern.

## Configuration

```env
AO_A2A_ROUTING=litellm_proxy
AO_LITELLM_PROXY_URL=http://litellm:4000
AO_LITELLM_PROXY_API_KEY=sk-alstom-master          # or:
AO_LITELLM_PROXY_API_KEY_REF=env:AO_LITELLM_VIRTUAL_KEY
AO_LITELLM_AGENT_PREFIX=/a2a
AO_LITELLM_A2A_PROTOCOL_VERSION=0.3                 # 0.3 | 1.0
AO_LITELLM_FORWARD_TRACE_HEADERS=true
```

The key is mandatory in proxy mode; a missing key fails fast with
`missing_proxy_key` instead of silently calling the wrong endpoint.

## Agent naming

The gateway addresses agents by the name they were registered under. Resolution
order:

1. `card.metadata.litellm_agent_name`
2. `card.agent_id`

So an existing card keeps working unchanged if the two names match. Use the
metadata field when the gateway name differs (e.g. `ij-local`).

## Wire details

| Concern | Proxy mode |
|---|---|
| Invocation | `POST {proxy}/a2a/{agent}` with `method: message/send` |
| Discovery | `GET {proxy}/a2a/{agent}/.well-known/agent-card.json`, then `/.well-known/agent.json` |
| Health probe | same discovery path (a card response means healthy) |
| Auth headers | `x-litellm-api-key: Bearer <key>` **and** `Authorization: Bearer <key>` |
| Version header | `A2A-Version: <AO_LITELLM_A2A_PROTOCOL_VERSION>` |
| Trace grouping | `X-LiteLLM-Trace-Id: <our trace id>`, `X-LiteLLM-Agent-Id: <agent name>` |
| Our own context | `X-Alstom-*` headers and `params.metadata` are still sent |
| Legacy fallback | disabled — the gateway has a fixed method set |

Because `X-LiteLLM-*` headers reach the agent, agent-side LLM calls made back
through LiteLLM group under the same trace, which is what makes per-run cost
attribution usable.

> **SDK note:** we deliberately speak JSON-RPC over HTTP rather than using
> `litellm.a2a_protocol`. That SDK module targets the pre-1.0 `a2a-sdk` API
> (`MessageSendParams`, `a2a.client.A2AClient`) which `a2a-sdk` 1.x removed; the
> gateway routes themselves require `a2a-sdk >= 1.1`. Talking to the HTTP surface
> avoids pinning a conflicting SDK in the orchestration service.

## Registering agents in LiteLLM

```yaml
# litellm/config.yaml
agents:
  - agent_name: finance-invoice-auditor
    agent_card_params:
      name: Finance Invoice Auditor
      url: http://mock-agents:9101/agents/finance
      version: 2.3.0
      protocolVersion: "0.3"
    litellm_params:
      api_key: os.environ/ALSTOM_AO_AGENT_KEY
```

Two registrations (our registry + the gateway) drift over time. Keep ours
authoritative and regenerate the gateway block:

```bash
python scripts/export_litellm_agents.py \
  --base-url http://localhost:8080 \
  --user-id admin --roles platform-admin \
  --out litellm/agents.generated.yaml
```

## Budgets across the two layers

The diagram's "session budgets" refers to LiteLLM's own budget system. It is a
different thing from this service's per-run budget, and the two must be configured
so they do not fight.

### What LiteLLM enforces

| Mechanism | Knob | Scope | On exceed |
|---|---|---|---|
| Global proxy budget | `litellm_settings.max_budget` + `budget_duration` | whole proxy | 429 |
| Team budget | `max_budget` / `budget_duration` on the team | team | 429 |
| Team-member budget | `max_budget_in_team` | user inside a team | 429 |
| Key budget | `max_budget`, `budget_duration`, `soft_budget` | one virtual key | 429 (soft: alert only) |
| **Agent session iterations** | `max_iterations` in the agent's `litellm_params` | LLM calls per `x-litellm-trace-id` session | **429** |
| **Agent session budget** | `max_budget_per_session` | dollars per session | **429** |
| Budget alerts | `alerting: [slack]`, `SLACK_WEBHOOK_URL`, `alert_types: [budget_alerts]` | key/user budgets | Slack message |

Two operational details matter:

* **Budgets require a database.** On a DB-less proxy the global budget check is
  skipped (fails open) and key/team budgets cannot resolve at all.
* **Session counters expire.** `max_iterations` / `max_budget_per_session`
  counters TTL out after `LITELLM_MAX_ITERATIONS_TTL` /
  `LITELLM_MAX_BUDGET_PER_SESSION_TTL` (default 3600 s). A run paused overnight for
  approval would come back with a reset meter.

Session tracking reuses the trace id we already propagate: our adapter sends
`X-LiteLLM-Trace-Id: <run trace id>`, so a gateway "session" is one of our runs.
`max_iterations` additionally requires `require_trace_id_on_calls_by_agent: true`
on that agent, and `require_trace_id_on_calls_to_agent: true` rejects a caller that
forgets the header (400).

### Why we leave them unset

Every gateway budget is a **hard 429**, and it lands on the *agent's own model
calls* mid-run. That is precisely the cascade this design avoids: the parent has
already paid for planning, the sub-agent dies half-way, and the user gets an error
instead of the partial result.

Our layer instead warns, downgrades, and only then refuses *new* work while still
returning agent answers that were already paid for
([budgets.md](budgets.md)).

### Recommended split

| Layer | Owns | Suggested values |
|---|---|---|
| LiteLLM | organisation-wide ceilings, per-key/team spend, dashboards, soft alerts | team/key `max_budget` per month; `soft_budget` for the 80% warning |
| This service | per-run fairness, loop caps, graceful partial answers | `AO_LLM_MAX_*`, `AO_MAX_AGENT_HOPS`, `AO_MAX_A2A_DEPTH` |

If you do want gateway backstops per run, set them **well above** the orchestration
cap (e.g. `max_budget_per_session` = 3× `AO_LLM_MAX_COST_USD_PER_RUN`) so the
graceful layer always trips first and the gateway only catches a runaway process
that bypasses it.

```yaml
# litellm/config.yaml (illustrative - not enabled in the demo stack)
litellm_settings:
  max_budget: 500
  budget_duration: 30d
general_settings:
  alerting: [slack]
  alert_types: [budget_alerts]
agents:
  - agent_name: finance-invoice-auditor
    agent_card_params: { ... }
    litellm_params:
      require_trace_id_on_calls_by_agent: true   # needed for session counters
      max_iterations: 60                          # backstop, ~3x our hop cap
      max_budget_per_session: 1.50                # backstop, ~3x AO_LLM_MAX_COST_USD_PER_RUN
```

### Current state of this repo

`litellm/config.yaml` sets no budgets — only `master_key` and
`store_prompts_in_spend_logs`. The gateway therefore **enforces nothing** today;
"session budgets" in the diagram is a capability, not an active policy.

## Verifying

```bash
# gateway sees the agent
curl -s localhost:4000/v1/agents -H "Authorization: Bearer $LITELLM_MASTER_KEY"

# gateway serves the proxied card
curl -s localhost:4000/a2a/finance-invoice-auditor/.well-known/agent.json

# our health probe goes through the gateway in proxy mode
curl -s -X POST localhost:8080/v1/agents/finance-invoice-auditor/health \
  -H 'X-User-Id: admin' -H 'X-User-Roles: platform-admin'

# a run stores the gateway trace id on every span
curl -s localhost:8080/v1/runs/<run_id>/trace -H 'X-User-Id: alice' \
  | jq '.events[] | select(.agent_id != null) | {action, agent_id, trace_id}'
```

## Switching back

Set `AO_A2A_ROUTING=direct`. Nothing else changes: cards already carry their own
`endpoint` and `auth`, and every test covers both modes.
