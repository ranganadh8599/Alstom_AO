# LibreChat integration

The orchestration layer is consumed through **LibreChat** as a virtual model:
LibreChat shows an OpenAI-compatible endpoint and curated model spec, AO decides
which agents to run, enforces OPA policy, pauses for human approval and traces to
Langfuse.

```
Browser ──► LibreChat :3080 ──► AO :8080 /v1 ──► agents (A2A)
                                     └──► Langfuse
```

## What is configured where

| File | Purpose |
|---|---|
| `LibreChat/librechat.yaml` | custom endpoint `alstom-ao`, model spec "Alstom Agents", starters, header mapping |
| `LibreChat/docker-compose.override.yaml` | mounts the config, joins the `alstom-ao-net` network, allowlists the AO host |
| `Alstom_AO/docker-compose.net.yml` | attaches the AO API to `alstom-ao-net` |
| `scripts/check_librechat_integration.py` | non-interactive verification of the whole path |

## Bring it up

```bash
# 1. shared network (once)
docker network create alstom-ao-net

# 2. orchestration layer
cd Alstom_AO
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.net.yml up -d

# 3. chat UI
cd ../LibreChat
docker compose up -d      # http://localhost:3080
```

Verify without a browser:

```bash
cd Alstom_AO && python scripts/check_librechat_integration.py
```

## Identity: the one thing that needed care

LibreChat substitutes `{{LIBRECHAT_USER_*}}` into outbound headers, so AO receives
the real signed-in user:

```yaml
headers:
  X-User-Id: '{{LIBRECHAT_USER_ID}}'
  X-User-Email: '{{LIBRECHAT_USER_EMAIL}}'
  X-Service-Id: 'librechat'
  X-Use-Case: 'chat-ui'
  X-Workflow-Id: '{{LIBRECHAT_BODY_CONVERSATIONID}}'
```

`X-User-Roles` is deliberately **not** mapped. LibreChat's role is an application
role (`USER`/`ADMIN`) governing the chat UI, not a business role. Mapping it made
AO see `roles=["ADMIN"]`, which matched no agent's `allowed_roles`
(`finance`, `hr`, `employee`, …) and produced *"no agents are registered"*.

Business roles must come from the identity provider (LDAP groups, SAML
attributes). Until that is wired, AO applies `AO_DEFAULT_USER_ROLES`.

Defence in depth for exactly this class of mistake: `planner_catalog` now falls
back to the unfiltered catalog when role filtering would hide **every** agent, and
lets OPA deny per agent with a real reason (`role_not_permitted`) instead of
showing a misleading empty catalog.

## Verified end to end

Against `gpt-4o-mini` through the LiteLLM gateway, in the prod stack:

| Check | Result |
|---|---|
| LibreChat loads the custom endpoint + spec | model picker shows **Alstom Agents**, 4 starters on the landing |
| Read question through the UI | `completed`, finance agent, real answer in the thread |
| Write question through the UI | **pauses**, shows Approval ID, risk and reasons in the chat |
| Approval via API | `completed`, agent executed, cost attributed |
| Langfuse | trace URL returned; spans confirmed via the v2 observations API (`orchestrator.plan`, `agent.finance-invoice-auditor`, `orchestrator.synthesize`) |
| `check_librechat_integration.py` | 11/11 |

## Operational notes

- **Restart LibreChat after editing `librechat.yaml`.** The file is bind-mounted
  but read at startup; `docker compose restart api` reloads it.
- **Config version must match the image.** LibreChat v0.8.9 accepts
  `version: 1.3.17`; `1.3.19` logs "Outdated Config version".
- **Schedules need an explicit single-replica assertion** locally:
  `SCHEDULES_SINGLE_PROCESS=true`, otherwise the scheduler refuses to arm.
- **The virtual model has no sampling knobs.** `dropParams` removes
  temperature/top_p/max_tokens so per-run budgets and prompts stay server-side.
- **Approvals are API calls, not chat UI.** The thread shows the approval ID and
  the endpoint; a real deployment would add an approval action in the UI or an
  external queue.
