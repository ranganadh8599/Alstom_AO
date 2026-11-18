# Adding an MCP server and its agent card

MCP servers publish **tools**, not conversational agents. One card maps onto **one
tool on one server**. The call always goes through the LiteLLM MCP gateway — the
orchestrator never speaks MCP and never dials your server.

```
Alstom AO  ->  POST {gateway}/mcp-rest/tools/call  ->  LiteLLM  ->  your MCP server
               {server_id, name, arguments}
```

Three steps: register the server with the gateway, find the tool name, write the
card. The API key only ever touches step 1.

---

## Step 1 — Register the server with the gateway

Add an entry under `mcp_servers:` in [`litellm/config.yaml`](../litellm/config.yaml):

```yaml
mcp_servers:
  my_server:                                   # <- becomes metadata.mcp_server_id
    url: "https://my-mcp-host.example/mcp"
    transport: "http"                          # streamable HTTP (default)
    auth_type: "bearer_token"                  # Authorization: Bearer <value>
    auth_value: os.environ/MY_MCP_API_KEY
```

Other auth shapes, if your server expects them:

| Server expects | `auth_type` | Notes |
|---|---|---|
| `Authorization: Bearer <key>` | `bearer_token` | most common |
| `X-API-Key: <key>` | `api_key` | |
| no auth | `none` | omit `auth_value` |
| OAuth 2.0 (M2M) | `oauth2` + `oauth2_flow: client_credentials` | add `client_id`, `client_secret`, `token_url`, `scopes` |

Then put the secret in `.env` (gitignored) and pass it through Compose:

```env
MY_MCP_API_KEY=<your key>
```

```yaml
# docker-compose.yml, under services.litellm.environment
MY_MCP_API_KEY: ${MY_MCP_API_KEY:-}
```

> **Never put the key in the card.** Cards are stored in Postgres and returned by
> `GET /v1/agents`. The gateway injects the credential upstream.

Restart the gateway so it loads the new server:

```bash
docker compose up -d --force-recreate litellm
```

---

## Step 2 — Find the tool name and its parameter

Guessing this is the usual reason a card registers but never works.

```bash
export LITELLM_MASTER_KEY=sk-alstom-master      # see the hint below
python scripts/mcp_discover.py
```

```
gateway: http://localhost:4000
servers: 2

  my_server  transport=http  auth=bearer_token
    url: https://my-mcp-host.example/mcp

tools (1):

  search_items    [server: my_server]
    Search the corpus and return matching documents.
    parameters: query:string*, limit:integer
```

The `*` marks required parameters. Then narrow to one server:

```bash
python scripts/mcp_discover.py --server my_server
```

It prints a ready-to-paste `metadata` block.

### If you see `400 No connected db.`

The MCP routes resolve access grants from the gateway database. With
`STORE_MODEL_IN_DB=False` (this stack's setting) there is no DB, so only the
**master key** works. A virtual key answers 400. Use:

```bash
python scripts/mcp_discover.py --key sk-alstom-master
```

The AO service itself uses the master key (`AO_LITELLM_PROXY_API_KEY` in
Compose), which is why the app can call MCP tools while a virtual key cannot.

### If the server does not appear at all

The gateway could not parse the block. Check its logs:

```bash
docker compose logs litellm --tail 50 | grep -i mcp
```

### If the server appears but returns no tools

The gateway cannot reach it. Common causes:

| Symptom in `server_outcomes` | Cause |
|---|---|
| `http_status: 421` | upstream MCP SDK rejects the `Host` header (DNS-rebinding protection) |
| `http_status: 401/403` | wrong `auth_type` or key |
| `upstream_error` with no status | DNS/network — internal hostname not resolvable from the container |

Verify DNS from *inside* the gateway container, not from your laptop:

```bash
docker compose exec litellm python -c "import socket;print(socket.gethostbyname('my-mcp-host.example'))"
```

---

## Step 3 — Write the card

Copy [`examples/agent_cards/sinequa-search.json`](../examples/agent_cards/sinequa-search.json)
and change the marked fields:

```json
{
  "agent_id": "sinequa-ronaldo-search",
  "name": "Sinequa Search (Ronaldo)",
  "description": "Searches Sinequa enterprise content through the Ronaldo MCP server.",
  "protocol": "mcp",
  "endpoint": "https://dev-globalsearch.alstom.hub/mcp/Ronaldo_SinequaSearch",
  "capabilities": ["sinequa search", "ronaldo information"],
  "permissions": {
    "allowed_roles": ["user", "admin"],
    "allowed_actions": ["read", "query"]
  },
  "environment": "dev",
  "risk_level": "low",
  "metadata": {
    "mcp_server_id": "ronaldo_sinequa",
    "mcp_tool": "search_items",
    "mcp_argument": "query"
  }
}
```

| Field | Why it matters |
|---|---|
| `protocol` | must be `"mcp"`; it selects the adapter |
| `capabilities` | **the routing signal** — the planner matches the question against these, not against the tool |
| `environment` | must equal `AO_ENVIRONMENT`, or the planner ignores the card |
| `endpoint` | catalogue metadata only; kept in sync with the gateway config |
| `metadata.mcp_server_id` | the key under `mcp_servers:` in the gateway config |
| `metadata.mcp_tool` | the **upstream, unprefixed** tool name from step 2 |
| `metadata.mcp_argument` | which tool parameter receives the question (`query`, `question`, `q`…) |

Optional metadata:

| Key | Effect |
|---|---|
| `mcp_arguments` | static JSON object merged into the arguments, e.g. `{"limit": 5}` |
| `mcp_query` | fixed value instead of the request text — use when the tool needs an exact id |

Register it:

```bash
curl -X POST localhost:8080/v1/agents \
  -H 'Content-Type: application/json' \
  -H 'X-User-Id: admin' -H 'X-User-Roles: admin' \
  -d @examples/agent_cards/sinequa-search.json
```

---

## Step 4 — Prove it works, in this order

Each step isolates one layer, so a failure tells you where the problem is.

```bash
# 1. gateway -> upstream: is the tool callable at all?
curl -s -X POST localhost:4000/mcp-rest/tools/call \
  -H "x-litellm-api-key: Bearer sk-alstom-master" \
  -H 'Content-Type: application/json' \
  -d '{"server_id":"ronaldo_sinequa","name":"search_items","arguments":{"query":"Cristiano Ronaldo"}}'
```

```bash
# 2. AO -> card: does health see the tool?
curl -X POST localhost:8080/v1/agents/sinequa-ronaldo-search/health \
  -H 'X-User-Id: admin' -H 'X-User-Roles: admin'
# healthy | degraded (server up, tool name wrong) | unhealthy (server unreachable)
```

```bash
# 3. AO -> end to end
curl -X POST localhost:8080/v1/orchestrate \
  -H 'Content-Type: application/json' \
  -H 'X-User-Id: alice' -H 'X-User-Roles: user' \
  -d '{"query":"What do we know about Cristiano Ronaldo?"}'
```

Then the same question in LibreChat.

---

## Reading the failures

| Result | Meaning | Fix |
|---|---|---|
| `healthy` but answers are useless | tool ran, argument wrong | adjust `mcp_argument` or pin `mcp_query` |
| `degraded` | server reachable, tool name not found | re-run step 2 |
| `unhealthy` | server unreachable from the gateway | DNS, VPN, URL, or `auth_type` |
| `failed` — `Tool X not found` | the gateway does not know that tool | wrong `mcp_tool` |
| `failed` — `server_not_found` | wrong `mcp_server_id` | match the key in `mcp_servers:` |
| Planner picks no agent | `capabilities` do not match the question, or `environment` mismatch | widen `capabilities`; check `AO_ENVIRONMENT` |
| `denied` — `action_not_permitted` | the inferred action is not in `allowed_actions` | add it, or use a read-only phrasing |

---

## Why the card cannot carry the connection details

`endpoint` and `auth` on a card are **catalogue metadata**. Agent traffic goes
through the gateway, which holds the real URL and credentials. Two consequences:

* editing `auth` in a card changes nothing about how the call authenticates;
* a card never needs a secret, so `GET /v1/agents` is safe to expose internally.
