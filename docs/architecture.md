# Architecture

## Components

| Component | Responsibility |
|---|---|
| `api/` | HTTP surface: native orchestration, OpenAI-compatible model, registry, approvals, policy, runs, health |
| `orchestrator/` | LangGraph graph, planner, nodes, run lifecycle |
| `registry/` | Central card catalog, A2A import, planner catalog |
| `adapters/` | Protocol adapters (A2A today) behind one interface |
| `policy/` | OPA client + local parity rules |
| `approvals/` | Dynamic risk assessment + approval persistence |
| `llm/` | LiteLLM gateway, budgets, prompts |
| `observability/` | Langfuse spans + durable trace ledger + metrics |
| `security/` | Dual identity extraction, secret resolution |
| `db/` | Tables, repositories, session factory |

## Run sequence

```mermaid
sequenceDiagram
    participant UI
    participant API
    participant G as LangGraph
    participant R as Registry
    participant O as OPA
    participant X as A2A adapter
    participant W as LiteLLM agent gateway
    participant A as A2A Agent
    participant L as Langfuse/ledger

    UI->>API: POST /v1/orchestrate (X-User-*)
    API->>G: ainvoke(state, runtime)
    G->>R: planner catalog (env + roles)
    G->>G: plan (LLM + heuristic)
    G->>O: authorize each agent
    alt approval required
        G->>L: approval.requested
        G-->>API: interrupt
        API-->>UI: status=awaiting_approval
        UI->>API: POST /v1/approvals/{id}/decision
        API->>G: Command(resume=…)
    end
    G->>X: invoke(card, request)
    alt AO_A2A_ROUTING=litellm_proxy
        X->>W: POST /a2a/{agent} JSON-RPC + virtual key
        W->>A: message/send (upstream)
        A-->>W: task + artifacts
        W-->>X: normalized response
    else AO_A2A_ROUTING=direct
        X->>A: message/send (trace + identities)
        A-->>X: task completed + artifacts
    end
    X-->>G: normalized InvocationResult
    G->>L: agent.completed, synthesis, run.completed
    G-->>API: final state
    API-->>UI: response + run_id/trace_id
```

## State

`OrchestratorState` holds only serializable data: query, identities (dicts),
catalog snapshot, plan, **pinned card snapshots**, policy decisions, results,
failures, response, status, usage. Runtime objects (gateway, budget, tracer) are
passed through `config["configurable"]["runtime"]`, never checkpointed, and rebuilt
on resume from the persisted run.

## Terminal states

| Status | Meaning |
|---|---|
| `invalid` | Empty/oversized query |
| `no_match` | No registered agent can help; message lists what exists |
| `denied` | Policy denied every selected agent (enforce mode) |
| `awaiting_approval` | Paused; nothing executed |
| `rejected` | Human said no |
| `completed` | Answer synthesized |
| `failed` | All agents failed, or an internal/LLM failure |
| `timeout` | Run exceeded `AO_RUN_TIMEOUT_SECONDS` |

## Failure isolation

| Failure | Behavior |
|---|---|
| One agent fails/times out | Run continues; answer notes which agent failed |
| All agents fail | `failed` with an explicit message |
| LLM planner fails | Heuristic planner (source marked `heuristic-fallback`) |
| LLM synthesis fails | Deterministic concatenation of agent answers |
| Langfuse down | Ledger still written; tracing skipped |
| OPA down | Local policy fallback or fail-closed (configurable) |
| Budget exceeded | Run fails with a budget message; usage persisted |

## Concurrency

Agents fan out with `asyncio.gather` bounded by `AO_MAX_PARALLEL_AGENTS`.
`previous_agents`/`next_agents` reflect **plan order**, which is the deterministic
logical chain even when calls overlap; actual start/finish order is in the ledger
timestamp order.
