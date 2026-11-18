# Architecture review — Alstom AO end-to-end

Component-level view of the orchestration layer as deployed, with the
responsibility of every block and the file that implements it. Intended for
architecture review; each diagram is paired with a table so a reviewer can map a
box to code.

Deployed topology (verified running):

| Service | Port | Role |
|---|---|---|
| LibreChat | 3080 | end-user chat UI |
| Alstom AO API | 8080 | orchestration layer (this repo) |
| LiteLLM gateway | 4000 | LLM routing + A2A agent gateway |
| OPA | 8181 | policy decisions |
| Postgres | 5432 | registry, runs, trace ledger, approvals, Langfuse checkpoints |
| Mock A2A agents | 9101 | reference A2A peers (finance / HR / IT) |
| Langfuse | cloud | span-level tracing |

---

## 1. System context — who calls whom

```mermaid
flowchart TB
    subgraph users["Users"]
        U1["Employee<br/>browser"]
        U2["Approver<br/>API / queue"]
    end

    subgraph ui["Presentation"]
        LC["LibreChat :3080<br/>chat UI, model picker,<br/>forwards user identity"]
    end

    subgraph ao["Orchestration layer — Alstom AO :8080"]
        API["FastAPI surface<br/>/v1/chat/completions · /v1/orchestrate<br/>/v1/agents · /v1/approvals · /v1/runs"]
        GRAPH["LangGraph workflow<br/>7 nodes"]
        REG["Agent registry<br/>cards + lifecycle"]
        POL["Policy client<br/>OPA + local parity"]
        APP["Approval service<br/>risk → human gate"]
        GW["LLM gateway<br/>LiteLLM, per-run budget"]
        AD["A2A adapter<br/>JSON-RPC 2.0"]
        TR["Tracer<br/>Langfuse spans + ledger"]
    end

    subgraph infra["State & policy"]
        PG[("Postgres<br/>agent_cards · runs<br/>trace_events · approvals<br/>policy_decisions")]
        OPA["OPA :8181<br/>Rego decisions"]
    end

    subgraph llm["Model & agent plane"]
        LG["LiteLLM gateway :4000<br/>model routing, spend logs"]
        AG["A2A agents :9101<br/>finance · HR · IT"]
    end

    subgraph obs["Observability"]
        LF["Langfuse<br/>traces · spans"]
    end

    U1 --> LC
    U2 --> API
    LC -->|"OpenAI-compatible<br/>X-User-Id · X-User-Roles"| API
    API --> GRAPH

    GRAPH --> REG
    GRAPH --> POL
    GRAPH --> APP
    GRAPH --> GW
    GRAPH --> AD
    GRAPH --> TR

    REG --> PG
    APP --> PG
    TR --> PG
    POL -->|"POST /v1/data/alstom/authz/decision"| OPA

    GW -->|"chat/completions"| LG
    AD -->|"/a2a/{agent} or direct"| LG
    LG --> AG
    AD -. "no such path" .-x AG

    TR -.->|"OTLP spans"| LF
    OPA -.->|"policies/*.rego"| OPA
```

### Block responsibilities

| Block | What it does | Where |
|---|---|---|
| **LibreChat** | Chat UI; presents the orchestration layer as a model spec ("Alstom Agents") with conversation starters. Substitutes the signed-in user into outbound headers. Never decides routing. | `LibreChat/librechat.yaml` |
| **FastAPI surface** | Two entry shapes over the same engine: native (`/v1/orchestrate`) and OpenAI-compatible (`/v1/chat/completions`, streaming SSE). Extracts dual identity, validates input, exposes registry/approval/run/trace endpoints. | `src/alstom_ao/main.py`, `api/routers/*` |
| **LangGraph workflow** | The decision engine. Seven nodes, conditional exits on every terminal outcome. Details in §3. | `orchestrator/graph.py` |
| **Agent registry** | Source of truth for *who* an agent is and *what it may do*: identity, capabilities, schemas, permissions, version, risk, environment, health. Also produces the role-filtered planner catalog. | `registry/service.py`, `db/repositories.py` |
| **Policy client** | Per-agent authorization before any call. Calls OPA; falls back to a local Python mirror of the same rules when OPA is unreachable. Records every decision. | `policy/service.py`, `policies/*.rego` |
| **Approval service** | Computes whether a human must approve, from actions + card risk + policy obligations + deterministic intent inference. Persists the request; supports expiry and separation of duties. | `approvals/service.py` |
| **LLM gateway** | Single path to models. Provider-agnostic via LiteLLM, with per-run budget, retries, fallbacks, and a deterministic mock for hermetic tests. | `llm/gateway.py` |
| **A2A adapter** | Calls agents over JSON-RPC 2.0 **through the LiteLLM gateway only**. Handles discovery, gateway auth, retries, idempotent ids and trace propagation. There is no direct path to an agent. | `adapters/a2a.py` |
| **Tracer** | One Langfuse trace per run with an `agent` span per invocation, plus an append-only `trace_events` ledger that survives Langfuse outages. | `observability/tracing.py` |
| **Postgres** | Registry + operational ledger + paused-run checkpoints + approval queue. Single durable store; SQLite in dev. | `db/tables.py` |
| **OPA** | Rego decisions: agent authorization and approver eligibility. Fail-closed option. | `policies/` |
| **LiteLLM gateway** | Model routing/spend logs for LLM calls; optional agent gateway (`/a2a/{agent}`) that owns upstream agent credentials. | `litellm/config.yaml` |
| **Langfuse** | Trace inspection for operators: spans, latency, token/cost per span. | external |

---

## 2. End-to-end request lifecycle

```mermaid
sequenceDiagram
    autonumber
    participant U as User (LibreChat)
    participant A as AO API
    participant G as LangGraph
    participant R as Registry
    participant P as OPA
    participant M as LiteLLM
    participant X as A2A adapter
    participant AG as Agent
    participant T as Tracer
    participant DB as Postgres

    U->>A: POST /v1/chat/completions<br/>X-User-Id, X-User-Roles
    A->>A: extract dual identity, normalise roles,<br/>validate payload, input guardrails
    A->>DB: insert run (status=pending) — before any work
    A->>T: run.started
    A->>G: ainvoke(state, runtime)

    G->>R: planner catalog (env, roles)
    G->>M: planner call (closed catalog)
    M-->>G: plan JSON (agents, actions, confidence)
    G->>G: validate plan against registry<br/>(drop unknown agents/actions)
    G->>T: plan.created

    loop each selected agent
        G->>P: authorize(user, agent, action)
        P-->>G: allow/deny + obligations
        G->>T: policy.evaluated
    end

    alt human approval required
        G->>DB: persist approval (pending, TTL)
        G->>T: approval.requested
        G-->>A: interrupt
        A-->>U: status=awaiting_approval + reasons
        U->>A: POST /v1/approvals/{id}/decision
        A->>A: verify approver role, no self-approval, TTL
        A->>G: resume(decision)
        G->>T: approval.decided
    end

    par fan-out (bounded parallelism)
        G->>X: invoke(agent 1, trace+identity)
        X->>AG: message/send
        AG-->>X: task + artifacts
        X-->>G: InvocationResult
    and
        G->>X: invoke(agent 2, …)
    end
    G->>T: agent.invoked / agent.completed / agent.failed
    G->>G: validate output schema

    G->>M: synthesizer call (agent results only)
    M-->>G: answer
    G->>G: numeric grounding (off/warn/strict)
    G->>G: output guardrails
    G->>T: synthesis.completed, run.completed
    A->>DB: persist status, plan, results, usage
    A-->>U: answer (+ usage, run_id, langfuse_url)
```

---

## 3. Inside the orchestration graph

```mermaid
flowchart LR
    S([START]) --> V["validate"]
    V -->|"empty / too long /<br/>guardrail blocked"| INV([invalid])
    V --> P["plan"]
    P -->|"no agent matches"| NM([no_match])
    P --> AU["authorize"]
    AU -->|"all denied"| DN([denied])
    AU --> AP{"approval<br/>required?"}
    AP -->|"no"| EX["execute"]
    AP -->|"yes"| HU["human<br/>interrupt"]
    HU -->|"rejected / expired"| RJ([rejected])
    HU -->|"approved"| EX
    EX -->|"hop cap / budget<br/>exhausted"| FL["finalize"]
    EX --> SY["synthesize"]
    SY --> FL
    FL --> E([END])
```

| Node | Responsibility | Key decisions | File |
|---|---|---|---|
| **validate** | Reject unusable input before spending anything. Normalises query, enforces length cap, runs input guardrails (injection, secret requests, PII). | empty / too long / guardrail-blocked → `invalid` | `orchestrator/nodes.py` |
| **plan** | Ask the LLM planner (closed catalog) which agents to use; validate the answer against the registry; fall back to the deterministic heuristic when the model is unavailable, invalid, or low-confidence; merge multi-intent clauses. Pins the card **versions** into state. | no valid agent → `no_match` with the catalog described to the user | `orchestrator/planner.py` |
| **authorize** | Per agent: OPA decision on user + service + agent + action + environment. Fail-closed in `enforce`; records every decision for audit. | any denial removes that agent; all denied → `denied` | `policy/service.py` |
| **approval** | Deterministic risk assessment: destructive/financial actions always pause; card `risk_level` and `requires_approval_for`; policy obligations; planner opinion is advisory only for read-only plans. Persists the request, then `interrupt()`s. | paused → `awaiting_approval`; resume → execute or `rejected` | `approvals/service.py` |
| **execute** | Guards (delegation depth, hop cap, budget), then bounded fan-out over the selected agents. Validates each result against the card's `output_schema`. Never fails the whole run for one agent. | hop/budget stop → `finalize` with its own message | `orchestrator/nodes.py`, `adapters/a2a.py` |
| **synthesize** | One model call over agent results only, with numeric grounding (repair → redact → verbatim) and output guardrails. If the budget is spent, returns concatenated agent answers instead of discarding paid work. | — | `llm/grounding.py`, `llm/guardrails.py` |
| **finalize** | Terminal ledger entry; persists usage and the final status. Runs for every terminal path. | — | `orchestrator/nodes.py` |

---

## 4. Identity and trust boundaries

```mermaid
flowchart LR
    subgraph untrusted["Untrusted"]
        BR["Browser"]
    end
    subgraph edge["Edge — deployment responsibility"]
        AUTH["Authenticate user<br/>TLS · rate limit<br/>strip inbound X-User-*"]
    end
    subgraph trusted["Trusted"]
        LC["LibreChat"]
        AO["AO API"]
        OPA["OPA"]
        DB[("Postgres")]
    end

    BR -->|"credentials"| AUTH
    AUTH -->|"session"| LC
    LC -->|"X-User-Id<br/>X-User-Roles<br/>X-Service-Id<br/>X-Workflow-Id"| AO
    AO -->|"user + service + agent<br/>+ action"| OPA
    AO --> DB
```

**Invariants**

1. Two identities are preserved end to end: the **user** (from headers) and the **service** (calling component). The **agent** identity comes from the registry card, never from the caller.
2. AO trusts `X-User-*` because the UI owns authentication. Deployments must terminate auth at the edge and strip inbound copies, or set `AO_TRUST_FORWARDED_IDENTITY=false`.
3. Roles are canonicalised to `admin` / `user` at the boundary; unknown names are dropped and the presented values retained for audit.
4. Ownership is enforced on reads: a run is visible to its owner, and to `admin`. Tenancy is enforced for `user` callers.

---

## 5. Cross-cutting

```mermaid
flowchart TB
    REQ["Every request<br/>enters the same pipeline"]

    REQ --> G
    REQ --> C
    REQ --> L
    REQ --> A

    subgraph G["Guardrails"]
        G1["input<br/>injection · secrets · PII"]
        G2["output<br/>PII masking"]
        G1 --> G2
    end

    subgraph C["Contracts"]
        C1["plan validated<br/>against registry"]
        C2["agent result validated<br/>against output_schema"]
        C3["synthesized figures<br/>grounded in agent output"]
        C1 --> C2 --> C3
    end

    subgraph L["Limits"]
        L1["per-run LLM<br/>calls · tokens · cost"]
        L2["agent hops ·<br/>inbound A2A depth"]
        L3["run timeout ·<br/>agent timeout + retries"]
        L4["idempotency key<br/>→ one run"]
    end

    subgraph A["Audit"]
        A1[("trace_events ledger<br/>durable")]
        A2["Langfuse spans<br/>visual"]
        A3[("policy_decisions")]
        A1 -.-> A2
    end
```

| Concern | Mechanism | Mode switch |
|---|---|---|
| Input/output guardrails | injection + secret-request block; PII/profanity warn; output PII masked | `AO_GUARDRAILS_MODE` |
| Output contracts | `jsonschema` Draft 2020-12 against card schema; prose = not applicable; external `$ref` rejected at registration | `AO_OUTPUT_SCHEMA_MODE` |
| Numeric grounding | every figure must exist in agent output or the question; repair → redact → verbatim | `AO_SYNTHESIS_GROUNDING` |
| Budgets | soft alert (alert + model downgrade) → hard gate that still returns collected results | `AO_LLM_MAX_*`, `AO_BUDGET_*` |
| Loop caps | agent hops per run; inbound delegation depth | `AO_MAX_AGENT_HOPS`, `AO_MAX_A2A_DEPTH` |
| Idempotency | `Idempotency-Key` + unique constraint; replay returns the original run | header |
| Access control | owner / admin scoping on runs, traces, approvals | `security/access.py` |
| Tracing | one trace per run, `agent` span per invocation, ledger as fallback | `AO_LANGFUSE_*` |
| Retention | `scripts/prune_trace_events.py`, never prunes pending work | CLI |

---

## 6. Failure and terminal states

| State | Trigger | What the user sees | Agent calls |
|---|---|---|---|
| `invalid` | empty/oversized query, guardrail block | specific reason | 0 |
| `no_match` | no registered agent fits | catalog hint of what exists | 0 |
| `denied` | policy denied all agents | which agents, which rule | 0 |
| `awaiting_approval` | consequential action | approval ID, risk, reasons | 0 |
| `rejected` | human said no | rejection note | 0 |
| `completed` | synthesis succeeded | answer (+ partial note if some agents failed) | n |
| `failed` | all agents failed, budget/hop stop, internal error | honest message, never fabricated | 0..n |
| `timeout` | run exceeded wall clock | timeout message | ≤n |

Design rule: **a terminal state is always persisted and always explained**; the reply never claims work that did not happen.

---

## 7. Questions for the reviewer

1. **Inbound A2A is not implemented.** Agent → orchestrator delegation has no server surface, so `AO_MAX_A2A_DEPTH` currently guards nothing. Is multi-hop mesh in scope, and if so who owns the agent card we publish?
2. **Approval is API-only.** The chat shows the approval ID and endpoint; there is no in-UI approve action. Acceptable for the pilot, or blocking?
3. **Two roles only (`admin`/`user`).** Business roles are not modelled; per-role agent filtering is effectively off for UI users. Confirm this is acceptable until the IdP mapping exists.
4. **`admin` is global and the sole approver.** That removes separation of duties except "cannot approve your own request". A single-admin org cannot approve its own writes. Intended?
5. **Agent credential custody.** `direct` mode holds agent secrets in AO; `litellm_proxy` mode moves them to the gateway. Which is the production stance?
6. **Honesty controls default to `warn`.** Grounding can be `strict`; guardrails can `block`. Which defaults should ship, given they change user-visible behaviour?
7. **No rate limiting** in AO. Should this live at the edge (ingress) or in the service?
8. **Agent accuracy is out of scope** of the proxy: grounding compares answer ↔ agent, so a wrong agent number is surfaced (cross-agent disagreement) but not corrected. Is upstream agent verification owned elsewhere?
