# Production readiness

This is the checklist the platform was audited against, with what is covered and
what is deliberately left to the deployment.

Audit method: static analysis (`ruff`, `mypy`), 101 hermetic tests, a live
end-to-end demo, real OPA evaluation, a real Docker build and container health
check, a Postgres + OPA + mock-agent Compose stack, and adversarial probes that
tried to break the invariants below.

**Verdict:** the service is fit for a controlled production rollout after the
go-live checklist in section 8. The remaining gaps are deployment responsibilities
(edge auth, rate limiting, TLS) and deliberately deferred features (section 7),
not defects.

---

## 1. Correctness invariants

| Invariant | Enforced by | Verified by |
|---|---|---|
| No agent runs unless registered, active, in-environment, policy-allowed | `plan` → `authorize` nodes | `test_policy.py`, `test_orchestration.py` |
| A planned version is the executed version | card snapshots pinned in graph state | `test_approvals.py` |
| No answer is invented when no agent matches | `no_match` terminal state | `test_orchestration.py` |
| Destructive actions always pause for a human | `assess_approval` hard guardrails | `test_approvals.py` |
| A rejected approval executes nothing | `approval` terminal branch | `test_approvals.py` |
| A retried submission never re-executes | `Idempotency-Key` + unique constraint | `test_production_readiness.py` |
| A lost checkpoint fails loudly, never silently | `RunService.ensure_resumable` | `test_production_readiness.py` |
| Callers cannot read other tenants' runs/approvals | `security/access.py` | `test_access_control.py` |

### Two failure modes closed during the audit

1. **Duplicate submission re-executed an agent.** A repeated POST with the same
   `Idempotency-Key` created a second run and called the agent twice. Now the
   second request returns the original run; a DB unique constraint
   `(user_id, idempotency_key)` closes the concurrent race.
2. **A lost checkpoint faked success.** After a restart without a durable
   checkpointer, approving a paused run returned HTTP 200, marked the approval
   approved, and executed nothing. Now it returns
   `409 run_state_unavailable`, leaves the approval `pending`, and logs an
   error at startup when a production deployment falls back to `MemorySaver`.

---

## 2. Security

| Control | State |
|---|---|
| Identity from trusted headers, 401 without `X-User-Id` | implemented |
| Service identity + token fingerprint (never the token) | implemented |
| Agent secrets referenced as `env:NAME`, never stored in cards | implemented |
| Secrets never logged (log formatter redacts) | implemented |
| Object-level authorization on runs/approvals (BOLA) | implemented |
| Tenant-scoped approvers, global platform admins only | implemented |
| Policy fail-closed option (`AO_POLICY_MODE=enforce` + no local fallback) | implemented |
| Approval separation of duties (no self-approval) | implemented |
| Registry writes restricted to `platform-admin`/`registry-admin` | implemented |
| Metrics scrape token | implemented (`AO_METRICS_TOKEN`) |
| No committed secrets | verified by scan |
| **Edge authentication** (JWT/OIDC validation) | **deployment responsibility** |
| **Rate limiting / WAF** | **deployment responsibility** (put it in front) |
| **TLS** | **deployment responsibility** |

`X-User-*` headers are trusted by design because the UI owns authentication.
Terminate auth at the edge and strip client-supplied copies, or set
`AO_TRUST_FORWARDED_IDENTITY=false`.

---

## 3. Reliability

| Concern | State |
|---|---|
| Per-run timeout | `AO_RUN_TIMEOUT_SECONDS`, terminal `timeout` status |
| Per-agent timeout + retries with backoff | `AO_A2A_TIMEOUT_SECONDS`, `AO_A2A_MAX_RETRIES` |
| One failing agent does not fail the run | partial answer with explicit failure note |
| LLM provider outage | retries, fallback models, deterministic synthesis fallback |
| LLM budget | token/call/cost ceilings per run, persisted across resume |
| Durable pause/resume | Postgres checkpointer (`postgres` extra) |
| Health/readiness | `/healthz`, `/readyz` (DB, adapters, OPA, Langfuse, LLM mode) |
| Crash-consistent run record | run written before the graph starts; terminal state always persisted |

**Known limit:** with `MemorySaver` (no Postgres extra) an approval pause is
in-memory. The platform now refuses to resume rather than pretending success, but
the operator-visible symptom is a stuck run — use Postgres in production.

---

## 4. Data lifecycle

| Concern | State |
|---|---|
| Schema migrations | Alembic, two revisions, up/down verified on SQLite |
| Append-only audit ledger | `trace_events`, `policy_decisions`, `runs`, `approvals` |
| Retention | `scripts/prune_trace_events.py --days N` (`--dry-run`, `--stats`, batching) |
| Pending work never pruned | retention skips `pending`/`executing`/`awaiting_approval` |
| Indexes for hot queries | run status/user/tenant/trace, approval status/run, event run/trace/time |

---

## 5. Operability

| Concern | State |
|---|---|
| Structured JSON logs with request id, redaction | implemented |
| Prometheus metrics (counters + latency summary) | implemented |
| OpenTelemetry/Langfuse traces with agent spans | implemented |
| Dockerfile with healthcheck | implemented, runs as `appuser` (uid 10001) |
| Docker Compose with Postgres, OPA, LiteLLM, mock agents | implemented and exercised end to end |
| CI (lint + tests + OPA check) | `.github/workflows/ci.yml` |
| Demo / smoke path without API keys | `scripts/run_demo.py` |

---

## 6. Test evidence

### Hermetic suite

```
101 tests, SQLite + fake A2A network + mock/scripted LLM + fake LiteLLM gateway
coverage ~82% of src/alstom_ao
ruff: clean
mermaid diagrams: validated with mermaid.parse (4/4)
OPA: `opa check` clean on openpolicyagent/opa:1.22.4
migrations: upgrade -> head and downgrade -> base verified on SQLite
```

### Live verification (not simulation)

| Check | Result |
|---|---|
| Docker image build | succeeded; container reports `healthy` |
| Non-root runtime | `whoami` inside container → `appuser` |
| `/readyz` with Postgres + OPA | `status: ok`, `database: ok`, `opa: healthy`, `mode: enforce` |
| Read query end-to-end | routed to `finance-invoice-auditor`, `status: completed`, policy `source: opa`, `allowed: true` |
| **Durable pause across container restart** | run paused, `docker compose restart api`, approval still `pending`, approved afterwards → `completed`, agent executed |
| OPA enforcement denial | `status: denied`, reason `service_scope_missing`, `source: opa`, no agent invoked |
| Idempotency in container | two identical POSTs with the same key returned the same `run_id` |
| Object-level access control in container | tenant-B caller → `403`; owner → `200` |
| Agent unreachable (mock agents down) | graceful `failed` with a user-facing message, not a 500; ledger recorded `agent.failed` |

### Type checking

`mypy` reports 60 findings, almost all pydantic enum-coercion (`str` where an
`Enum` is annotated) plus SQLAlchemy `Result.rowcount` typing gaps — false
positives at runtime. The codebase is **not** `--strict` clean and this document
does not claim otherwise.

---

## 7. Deliberate non-goals

These exist in the reference projects but are intentionally out of scope here;
add them only with a concrete requirement.

| Feature | Reference | Why omitted |
|---|---|---|
| Deterministic mid-run node replay | loomvault | LangGraph checkpoints cover resume; full journal replay is a different execution model |
| Content-addressed LLM response cache | loomvault | Cost optimisation; add when spend justifies it |
| Budget `DEGRADE`/`TRUNCATE` strategies | loomvault | Replaced, not copied: we implement soft-alert → model downgrade → hard-gate ([budgets.md](budgets.md)) and reject `TRUNCATE`, which silently changes answer quality. Worth noting: loomvault documents `TRUNCATE` but its engine never implements it, and its `WARN` sets the degrade flag, so only `FAIL` and `DEGRADE` are real there. |
| Mid-stream pause on budget exhaustion | — | Rejected: a frozen A2A task needs a long-lived state machine across callbacks and timeouts. A hard gate at the graph boundary is cheaper and safer. |
| Leases, heartbeats, dead-letter queues | orloj | Single-process worker model; needed only for distributed workers |
| SSO (JWT/Azure AD) | advanced template | The UI owns authentication; add a verifier if agents are exposed directly |
| Team/organisation model | advanced template | Tenant id covers isolation; a team entity is a product decision |
| Agent-level streaming to the client | awslabs | Chat-level SSE implemented; per-agent stream fan-in is a UI concern |

---

## 8. Go-live checklist

1. `AO_ENVIRONMENT=prod` (startup now refuses demo seeding; `debug` is rejected).
2. `AO_DATABASE_URL=postgresql+asyncpg://…` and `alembic upgrade head`.
3. Install the `postgres` extra so pauses are durable; confirm the startup log is
   free of the `MemorySaver in production` error.
4. `AO_POLICY_MODE=enforce`, real `AO_OPA_URL`, then dry-run
   `POST /v1/policies/evaluate` for each agent.
5. Set `AO_LANGFUSE_*` and confirm `run.langfuse_url` appears.
6. Set `AO_METRICS_TOKEN`; scrape `/metrics`.
7. Front the service with authentication, rate limiting and TLS; strip inbound
   `X-User-*`.
8. Set `AO_LITELLM_PROXY_URL` and a gateway key. Agents are reachable **only**
   through the gateway; regenerate its config from the registry with
   `scripts/export_litellm_agents.py`.
9. Schedule `scripts/prune_trace_events.py`; alert on `GET /v1/approvals` age.
10. Send `Idempotency-Key` from the UI submit path.
