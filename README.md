# Alstom AO — Agent Orchestration Layer

An orchestration layer that sits between a chat UI and your enterprise agents.
The UI calls it **as if it were a single LLM** (`/v1/chat/completions`); it decides
which agents to run, enforces policy, pauses for human approval when an action is
consequential, and records a trace of everything that happened.

> **No agent is called unless it is registered, active, in the right environment,
> allowed by policy, and — when required — approved by a human.**

---

## Contents

1. [Prerequisites](#1-prerequisites)
2. [Quickstart (5 minutes, no Docker)](#2-quickstart-5-minutes-no-docker)
3. [Full stack with Docker](#3-full-stack-with-docker)
4. [Register your first agent](#4-register-your-first-agent)
5. [Run a request](#5-run-a-request)
6. [Approvals](#6-approvals)
7. [Configuration](#7-configuration)
8. [Project layout](#8-project-layout)
9. [Verify the install](#9-verify-the-install)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. Prerequisites

| Requirement | Version | Needed for |
|---|---|---|
| Python | 3.11+ | everything |
| Git | any | cloning |
| Docker + Compose | any recent | the full stack (Postgres, OPA, LiteLLM) |

Check what you have:

```bash
python --version
docker --version
```

You do **not** need Postgres, OPA, or a model API key to start the app — SQLite is
the default and the agent gateway is only contacted when a run executes.

---

## 2. Quickstart (5 minutes, no Docker)

From a fresh clone on a new laptop:

```bash
# 1. Enter the project
cd Alstom_AO

# 2. Create an isolated environment
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# 3. Install the package and its dependencies
pip install -e ".[dev]"

# 4. Create your local configuration
#    Windows
copy .env.example .env
#    macOS / Linux
cp .env.example .env

# 5. Prove it runs
python scripts/smoke_check.py

# 6. Start the API
python -m alstom_ao
```

Open <http://localhost:8080/docs> for the interactive API reference.

`smoke_check.py` prints one line per endpoint. **All five must say `ok`.** If they
do, the app, its schema and its routers are working.

> Stop the server with `Ctrl+C`.

---

## 3. Full stack with Docker

This starts Postgres, OPA (policy), LiteLLM (agent gateway) and the API together.

```bash
docker compose up --build
```

| Service | URL | Purpose |
|---|---|---|
| `api` | <http://localhost:8080> | this application |
| `api` docs | <http://localhost:8080/docs> | interactive API reference |
| `opa` | <http://localhost:8181> | policy decisions |
| `litellm` | <http://localhost:4000> | agent gateway + model routing |
| `postgres` | `localhost:5432` | database (`alstom` / `alstom`) |
| `reference-agent` | <http://localhost:9101> | one test A2A agent (not part of the platform) |
| `mcp-reference` | <http://localhost:9102> | one test MCP server, 3 tools (not part of the platform) |

Check everything is healthy:

```bash
curl -s localhost:8080/healthz
curl -s localhost:8080/readyz
```

Stop and remove everything, including data:

```bash
docker compose down -v
```

### Production-shaped run

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

The prod overlay switches to Postgres, applies schema through Alembic instead of
auto-create, and enforces OPA policy without a silent local fallback. Run the
migration first:

```bash
alembic upgrade head
```

---

## 4. Register your first agent

An agent must be registered before it can ever be called. Registration is
**admin-only**.

> **Every request needs identity headers.** The UI supplies them in production; when
> calling by hand, add them yourself: `X-User-Id` is required, `X-User-Roles`
> defaults to `user`.

### Try it first with the bundled reference agent

`docker compose up` starts one reference A2A agent on port 9101 (service
`reference-agent`). It is **not** part of the platform — it exists so a fresh
checkout has something to test against. Register it:

```bash
curl -X POST localhost:8080/v1/agents \
  -H 'Content-Type: application/json' \
  -H 'X-User-Id: admin' \
  -H 'X-User-Roles: admin' \
  -d @examples/reference_agent/card.json
```

It is `risk_level: high` and allows `write`, so it exercises the full path
without registering anything else: routing, OPA policy, the approval pause, and
execution. Try it with the queries in [section 5](#5-run-a-request).

To remove it once real agents are registered:

```bash
curl -X DELETE localhost:8080/v1/agents/finance-invoice-auditor \
  -H 'X-User-Id: admin' -H 'X-User-Roles: admin'
docker compose stop reference-agent
```

> The card's `environment` (`dev`) must match `AO_ENVIRONMENT`, or the planner will
> not see it. The dev compose stack sets `AO_ENVIRONMENT=dev`.

### Or try the bundled MCP server

The stack also runs a small **MCP** server on port 9102 (service `mcp-reference`)
with three read-only tools. MCP servers publish *tools*, not agents, so one card
maps onto one tool:

```bash
curl -X POST localhost:8080/v1/agents \
  -H 'Content-Type: application/json' \
  -H 'X-User-Id: admin' \
  -H 'X-User-Roles: admin' \
  -d @examples/reference_mcp/card.json
```

Then ask *"What is the status of invoice INV-2024-0912?"* — the planner can route
to the A2A agent and the MCP tool in the same run.

The card binds it with metadata:

```json
"protocol": "mcp",
"metadata": {
  "mcp_server_id": "alstom_reference",
  "mcp_tool": "invoice_status",
  "mcp_argument": "invoice_id"
}
```

LiteLLM is the MCP gateway: it owns the MCP session and the upstream credentials,
and the orchestrator calls it over REST (`/mcp-rest/tools/call`). It never speaks
MCP itself. Details: [docs/adapters.md](docs/adapters.md).

### Add your own MCP server

Four steps, in this order — each one isolates a layer, so a failure tells you
where the problem is:

```bash
# 1. register the server with the gateway (litellm/config.yaml -> mcp_servers:)
#    and put its key in .env, never in a card
docker compose up -d --force-recreate litellm

# 2. find the exact tool name and its parameter
LITELLM_MASTER_KEY=sk-alstom-master python scripts/mcp_discover.py --server my_server

# 3. register the card (copy examples/agent_cards/sinequa-search.json)
curl -X POST localhost:8080/v1/agents \
  -H 'Content-Type: application/json' \
  -H 'X-User-Id: admin' -H 'X-User-Roles: admin' \
  -d @examples/agent_cards/sinequa-search.json

# 4. prove each hop
curl -X POST localhost:8080/v1/agents/<agent_id>/health \
  -H 'X-User-Id: admin' -H 'X-User-Roles: admin'
```

Full walkthrough including every failure mode: **[docs/mcp-setup.md](docs/mcp-setup.md)**.

> MCP endpoints need the gateway **master key**: access grants are DB-backed and
> this stack runs `STORE_MODEL_IN_DB=False`, so a virtual key answers
> `400 No connected db.` The AO service already uses the master key.

### Register a real agent — card you wrote

```bash
curl -X POST localhost:8080/v1/agents \
  -H 'Content-Type: application/json' \
  -H 'X-User-Id: admin' \
  -H 'X-User-Roles: admin' \
  -d @examples/agent_cards/finance-invoice-auditor.json
```

The files in [`examples/agent_cards/`](examples/agent_cards) are **templates** —
edit the `endpoint`, `name` and `capabilities` to match your real agent.

### Register a real agent — import an existing A2A agent

If your agent already publishes an A2A card, let AO fetch it:

```bash
curl -X POST localhost:8080/v1/agents/discover \
  -H 'Content-Type: application/json' \
  -H 'X-User-Id: admin' \
  -H 'X-User-Roles: admin' \
  -d '{"endpoint":"https://your-agent.example/agents/finance","environment":"dev"}'
```

### Minimum required fields

```json
{
  "agent_id": "my-agent",
  "name": "My Agent",
  "description": "At least ten characters describing what this agent does.",
  "endpoint": "https://your-agent.example/agents/my",
  "capabilities": ["what this agent can do"]
}
```

Everything else has a default. The ones that change behaviour most:

| Field | Default | Effect |
|---|---|---|
| `environment` | `dev` | must equal the request environment or the planner ignores the card |
| `status` | `active` | `inactive` means never called |
| `risk_level` | `low` | `high` + a high-impact action triggers human approval |
| `permissions.allowed_roles` | `[]` | empty = any authenticated caller |
| `permissions.allowed_actions` | `[]` | empty = **read-only** (write/delete denied) |
| `permissions.requires_approval_for` | `[]` | actions that always pause for a human |
| `metadata.tenant_id` | unset | when set, callers from another tenant are denied |
| `metadata.mcp_server_id`, `mcp_tool` | unset | required when `protocol` is `mcp` |

### Wire the gateway

Registration alone does not make an agent callable — the LiteLLM gateway needs a
matching entry. Generate it from the registry:

```bash
python scripts/export_litellm_agents.py \
  --base-url http://localhost:8080 \
  --user-id admin --roles admin \
  --out litellm/agents.generated.yaml
```

If the gateway knows your agent by a different name, set
`metadata.litellm_agent_name` on the card.

### Manage agents

| Action | Command |
|---|---|
| List | `curl -s localhost:8080/v1/agents -H 'X-User-Id: alice'` |
| Read one | `curl -s localhost:8080/v1/agents/my-agent -H 'X-User-Id: alice'` |
| Update | `curl -X PATCH localhost:8080/v1/agents/my-agent -H 'X-User-Id: admin' -H 'X-User-Roles: admin' -H 'Content-Type: application/json' -d '{"description":"Updated description here."}'` |
| Deactivate | `curl -X DELETE localhost:8080/v1/agents/my-agent -H 'X-User-Id: admin' -H 'X-User-Roles: admin'` |
| Health probe | `curl -X POST localhost:8080/v1/agents/my-agent/health -H 'X-User-Id: alice'` |

`DELETE` is a **soft delete**: the card becomes `inactive` and history is kept.

---

## 5. Run a request

```bash
curl -X POST localhost:8080/v1/orchestrate \
  -H 'Content-Type: application/json' \
  -H 'X-User-Id: alice' \
  -H 'X-User-Roles: user' \
  -d '{"query":"Check invoice INV-2024-0912 for duplicate charges"}'
```

Or use the OpenAI-compatible surface, so any chat UI can point at it:

```bash
curl -X POST localhost:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'X-User-Id: alice' \
  -d '{"model":"alstom-ao","messages":[{"role":"user","content":"How many days of annual leave?"}]}'
```

### Follow what happened

```bash
curl -s localhost:8080/v1/runs  -H 'X-User-Id: alice'                    # list your runs
curl -s localhost:8080/v1/runs/<run_id> -H 'X-User-Id: alice'            # one run
curl -s localhost:8080/v1/runs/<run_id>/trace -H 'X-User-Id: alice'      # every step
curl -s localhost:8080/v1/runs/<run_id>/policy -H 'X-User-Id: alice'     # policy decisions
curl -s localhost:8080/v1/runs/<run_id>/timeline -H 'X-User-Id: alice'   # ordered events
```

### Submit safely, once

Send an `Idempotency-Key` header on submit. A retried request returns the original
run instead of executing again:

```bash
-H 'Idempotency-Key: 8f2c1a4e-...'
```

---

## 6. Approvals

When a high-risk agent is asked to perform a high-impact action
(`write`, `update`, `delete`, `send`, `execute`, `payment`), the run pauses as
`awaiting_approval` and nothing is executed.

```bash
# See what is waiting
curl -s localhost:8080/v1/approvals -H 'X-User-Id: approver' -H 'X-User-Roles: admin'

# Decide
curl -X POST localhost:8080/v1/approvals/<approval_id>/decision \
  -H 'Content-Type: application/json' \
  -H 'X-User-Id: approver' -H 'X-User-Roles: admin' \
  -d '{"approved":true,"note":"Checked against PO-4471"}'
```

Two rules are enforced by policy:

1. the approver must hold an approver-class role (`admin` by default);
2. **nobody may approve their own request** (separation of duties).

---

## 7. Configuration

All settings are environment variables prefixed `AO_`, read from `.env` or the
process environment. Environment variables win over `.env`.

### The ones you will actually change

| Variable | Default | Meaning |
|---|---|---|
| `AO_ENVIRONMENT` | `dev` | `dev` / `test` / `prod`. Cards must match this to be planned |
| `AO_DATABASE_URL` | `sqlite+aiosqlite:///./alstom_ao.db` | Postgres in production |
| `AO_DB_AUTO_CREATE` | `true` | set `false` in production and run `alembic upgrade head` |
| `AO_LITELLM_PROXY_URL` | `http://localhost:4000` | **required** — the only route to an agent |
| `AO_LITELLM_PROXY_API_KEY` | unset | gateway credential |
| `AO_LLM_MODEL` | `openai/gpt-4o-mini` | model used for planning and synthesis |
| `AO_POLICY_MODE` | `observe` | `observe` records denials; `enforce` blocks them |
| `AO_OPA_URL` | `http://localhost:8181` | policy engine |
| `AO_A2A_TIMEOUT_SECONDS` | `30` | per-agent call timeout |
| `AO_RUN_TIMEOUT_SECONDS` | `120` | whole-run timeout |
| `AO_MAX_PARALLEL_AGENTS` | `4` | fan-out ceiling |
| `AO_SYNTHESIS_GROUNDING` | `warn` | `off` / `warn` / `strict` numeric grounding |
| `AO_GUARDRAILS_MODE` | `warn` | `off` / `warn` / `block` prompt-injection and secrets |

### Production checklist

1. `AO_ENVIRONMENT=prod` and `AO_DEBUG=false` (startup refuses debug in prod).
2. `AO_DATABASE_URL=postgresql+asyncpg://…`, then `alembic upgrade head`.
3. `AO_DB_AUTO_CREATE=false`.
4. Install the Postgres extra so approval pauses survive a restart:
   `pip install -e ".[postgres]"`.
5. `AO_POLICY_MODE=enforce` with a reachable `AO_OPA_URL`.
6. Set `AO_METRICS_TOKEN` before exposing `/metrics`.
7. Terminate authentication at the edge and **strip inbound `X-User-*` headers**
   from untrusted clients — the app trusts them.

Full reference: [`docs/production.md`](docs/production.md).

---

## 8. Project layout

```
src/alstom_ao/
  api/            HTTP surface: routers, dependencies, middleware
  orchestrator/   LangGraph flow: validate -> plan -> authorize -> approval -> execute -> synthesize
  registry/       the agent catalogue (cards)
  policy/         OPA client, local fallback, decision model
  approvals/      human-in-the-loop requests and decisions
  adapters/       A2A and MCP transports to the gateway
  llm/            model gateway, budgets, grounding, guardrails
  db/             SQLAlchemy models, repositories, sessions
  security/       identity extraction and access control
  observability/  tracing, metrics, logs
alembic/          database migrations
policies/         OPA/Rego rules + their tests
examples/         reference agent cards
docs/             deeper documentation
scripts/          operational helpers
```

### Which doc for which question

| Question | Doc |
|---|---|
| What fields does an agent card have? | [`docs/agent-card.md`](docs/agent-card.md) |
| How does policy decide? | [`docs/policies.md`](docs/policies.md) |
| How do approvals work? | [`docs/approvals.md`](docs/approvals.md) |
| How is the gateway wired? | [`docs/litellm-a2a.md`](docs/litellm-a2a.md) |
| How do I add an MCP server and its card? | [`docs/mcp-setup.md`](docs/mcp-setup.md) |
| What are the budgets? | [`docs/budgets.md`](docs/budgets.md) |
| What does the API expose? | [`docs/api.md`](docs/api.md) |
| How does it fit together? | [`docs/architecture.md`](docs/architecture.md) |
| How do I wire a chat UI to it? | [`docs/librechat.md`](docs/librechat.md) |
| Is it production ready? | [`docs/production.md`](docs/production.md) |

---

## 9. Verify the install

```bash
# lint
make lint            # or: ruff check src examples

# boot the app and hit the critical endpoints
make check           # or: python scripts/smoke_check.py

# policy rules
opa check policies
```

Expected output of `make check`:

```
  ok   GET /healthz -> 200
  ok   GET /readyz -> 200
  ok   GET /v1/agents -> 200
  ok   GET /v1/agents/catalog -> 200
  ok   GET /v1/policies/status -> 200

5/5 endpoints healthy
```

---

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ValueError: AO_LITELLM_PROXY_URL is required` | gateway URL empty | set `AO_LITELLM_PROXY_URL` in `.env` |
| `401 Missing X-User-Id header` | no identity headers | add `-H 'X-User-Id: alice'` |
| `403 ... requires the admin role` | wrong role for a registry write | use `X-User-Roles: admin` |
| Register works but the planner picks no agent | `environment` mismatch | make the card's `environment` equal `AO_ENVIRONMENT` |
| `no such table` on first request | `AO_DB_AUTO_CREATE=false` with no migration | run `alembic upgrade head`, or set it `true` in dev |
| Agent never executes, run says `denied` | policy blocked it | check `/v1/runs/<id>/policy`; set `AO_POLICY_MODE=observe` while debugging |
| `missing_proxy_key` on a health probe | gateway key absent | set `AO_LITELLM_PROXY_API_KEY` |
| Port 8080 already in use | another server running | stop it, or set `AO_PORT` |

### Reset local state

The dev database is a single file:

```bash
# Windows
del alstom_ao.db
# macOS / Linux
rm alstom_ao.db
```

The app recreates it on the next start when `AO_DB_AUTO_CREATE=true`.

---

## License

MIT — see [LICENSE](LICENSE).
