# Guardrails, output contracts and evaluation

Three capabilities added after auditing the reference repositories: none of them
existed here, and only `nod` had anything comparable (eval-time URL grounding).

Motivation per repo:

| Repo | What it had | What we took |
|---|---|---|
| `nod` | `urlsGrounded()` eval check; failure taxonomy; `amounts()` normalization | Golden-set harness + failure taxonomy |
| `multi-agent-orchestator` | Guardrails AI `DetectPII`/`DetectJailbreak`/`ProfanityFree` | Local equivalents (no hub account needed) |
| `langgraph-complete-guide` | hardcoded `301`/`303` hallucination check | Rejected — not generalizable |
| `OPA` | `json.verify_schema` builtin | Not used; `jsonschema` in-process instead (see below) |

---

## 1. Content guardrails

`AO_GUARDRAILS_MODE`: `off` | `warn` (default) | `block`.

**Input** (untrusted user text):

| Category | Level | Notes |
|---|---|---|
| `prompt_injection` | **block** | "ignore previous instructions", "reveal your system prompt", `act as a different agent`, chat-template tokens |
| `secret_request` | **block** | asking for a password / OTP / API key |
| `pii` | warn | email, credit card, IBAN, SSN, international phone |
| `profanity` | warn | |

**Output** (anything leaving the platform): PII and profanity. In `block` mode PII
is masked (`[EMAIL_REDACTED]`), not dropped.

Deliberately conservative, because a guardrail that blocks legitimate work is
worse than none:

- credit-card detection requires a **Luhn checksum**, otherwise every 16-digit
  invoice id becomes "PII";
- PII is `warn`, not `block` — the user may legitimately be quoting their own
  address to get an answer about it;
- findings always name the category, so the ledger explains itself.

`guardrail.triggered` is recorded with phase, mode, blocked flag and findings.

### Why not Guardrails AI

The hub validators need an account and network access at boot. An orchestration
layer that must start on-prem cannot depend on that. The module keeps the same
shape (`check_input` / `check_output` returning findings), so a hub validator can
replace either function without touching the graph.

### Why not OPA's `json.verify_schema`

Attractive — the policy service is already called per agent — but it would put
payload-shaped data into every authorization request and require the schema to be
loaded as Rego data. Validating in-process keeps schemas with the card and works
when OPA is unreachable.

---

## 2. Output contract enforcement

Every card declares `output_schema`; nothing checked it. Now each agent result is
validated with `jsonschema` (Draft 2020-12).

`AO_OUTPUT_SCHEMA_MODE`: `off` | `warn` (default) | `enforce`.

| Status | Meaning | Effect in `enforce` |
|---|---|---|
| `valid` | payload matches | keep |
| `invalid` | payload violates the schema | **reject the agent result** (run continues with the others) |
| `not_applicable` | agent answered in prose | keep — A2A peers legitimately answer in prose |
| `schema_missing` | card declares no schema | keep, advisory |
| `schema_invalid` | the *card's* schema is broken | keep, reported once — a bad card must not fail every run |

Emitted as `agent.schema_violation` with the failing paths, plus
`ao.agents.schema_violation`.

---

## 3. Evaluation harness

`evals/golden_routing.json` holds 17 cases with expectations and a failure
taxonomy taken from nod's style: `routing_miss`, `routing_extra`,
`false_no_match`, `missed_no_match`, `wrong_status`.

```bash
# offline: deterministic heuristic, no keys, no network  (12 cases)
python -m evals.run_golden

# live: real LLM planner against the running stack      (17 cases)
python -m evals.run_golden --base-url http://localhost:8080
```

Non-zero exit on failure, so it works as a CI gate. `mode` per case says whether it
applies to the heuristic (`mock`), the real planner (`real`) or both.

Current status: **12/12 offline, 17/17 live** (gpt-4o-mini), including multi-agent
composition and the read-vs-write approval split.

### What it protects against

Prompt and planner changes are the riskiest edits in this codebase and had no
coverage. The golden set catches a routing regression in seconds offline, and in
minutes against the real model.

Add a case whenever a routing bug is found: the query, the agent that must be
chosen, and any agent that must *not* be (`must_not`).
