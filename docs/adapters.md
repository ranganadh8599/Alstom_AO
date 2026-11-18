# Adapters

## The contract

```python
class AgentAdapter(ABC):
    protocol: str
    supports_streaming: bool = False

    async def invoke(self, card: AgentCard, request: InvocationRequest) -> InvocationResult: ...
    async def check_health(self, card: AgentCard) -> AgentHealth: ...
    async def discover(self, endpoint: str) -> dict | None: ...
    async def aclose(self) -> None: ...
```

The orchestrator only knows `card.protocol → AdapterRegistry.get(...).invoke(...)`.
Adding MCP or REST is a new class plus one registration line; the graph, policy,
approval and tracing layers are untouched.

## A2A adapter

| Concern | Behavior |
|---|---|
| Routing | **Gateway-only in production**: `litellm_proxy` → `{gateway}/a2a/{agent}`. `direct` exists only for adapter unit tests and is refused when `AO_ENVIRONMENT=prod` |
| Discovery | `GET {base}/.well-known/agent-card.json`, then legacy `/.well-known/agent.json` |
| Invocation | JSON-RPC 2.0 `message/send` with `configuration.blocking=true` |
| Legacy peers | On HTTP 404/405/501, retries with `tasks/send` |
| Identity | `params.message.messageId` = `{run}-{agent}-{uuid}` (unique per attempt); task id reused for legacy |
| Trace | `params.metadata.trace_context`, `params.metadata.identity`, `X-Alstom-*` headers |
| Auth | `none`, `bearer`, `api_key` (secret resolved from `env:` refs); `oauth2` is rejected explicitly. In proxy mode a LiteLLM virtual key is used instead |
| Retries | Transport errors and 429/5xx, exponential backoff (`AO_A2A_MAX_RETRIES`) |
| Timeout | Per attempt (`AO_A2A_TIMEOUT_SECONDS`); timeout raises `agent_timeout` |
| Response | Merges `status.message.parts`, `artifacts[].parts`; maps `failed/canceled/rejected` to failure |
| Errors | JSON-RPC `error` → `AgentInvocationError`; never leaks into an unhandled 500 |

### Gateway invocation (the only mode)

Every invocation posts the JSON-RPC envelope to the LiteLLM agent gateway, adding `x-litellm-api-key`,
`A2A-Version`, `X-LiteLLM-Trace-Id` and `X-LiteLLM-Agent-Id`. The legacy
`tasks/send` fallback is disabled there because the gateway exposes a fixed method
set. See [litellm-a2a.md](litellm-a2a.md).

The card's `endpoint` is metadata for the gateway's own config (see
`scripts/export_litellm_agents.py`); the orchestrator never dials it.

### What a peer must expose

```
GET  /…/.well-known/agent-card.json
POST /…
{"jsonrpc":"2.0","id":"…","method":"message/send",
 "params":{"message":{"role":"user","messageId":"…","parts":[{"kind":"text","text":"…"}]},
           "metadata":{…},"configuration":{"blocking":true}}}
```

See `examples/agent_cards/` for reference card payloads.

## MCP adapter

MCP servers publish **tools**, not conversational agents. One registry card maps
onto **one tool on one server**, and the call goes through the LiteLLM MCP
gateway — the adapter never speaks MCP itself.

```
POST {AO_LITELLM_PROXY_URL}/mcp-rest/tools/call
{"server_id": "...", "name": "<tool>", "arguments": {...}}
```

| Concern | Behavior |
|---|---|
| Routing | **Gateway-only**: `{AO_LITELLM_PROXY_URL}{AO_MCP_TOOL_CALL_PATH}` |
| Discovery | `GET /mcp-rest/tools/list?server_id=…` through the gateway |
| Health | `healthy` if the tool is listed on the server, `degraded` if the server answers but lacks the tool, `unhealthy` if the server is unreachable |
| Identity | `X-Alstom-*` propagation headers, plus `X-LiteLLM-Trace-Id` / `X-LiteLLM-Agent-Id` |
| Auth | LiteLLM virtual key in `x-litellm-api-key`. Upstream MCP credentials live in the gateway (`auth_type`, OAuth, SigV4), never here |
| Retries | Transport errors, timeouts and 429/5xx with backoff (`AO_MCP_MAX_RETRIES`) |
| Response | Unwraps MCP `content[].text`, `structuredContent`, and LiteLLM `detail` error envelopes |
| Errors | `missing_mcp_server` / `missing_mcp_tool` raise immediately; gateway failures become a failed `InvocationResult` with a readable reason |

### Card metadata

| Key | Fallback | Meaning |
|---|---|---|
| `mcp_server_id` | `AO_MCP_DEFAULT_SERVER_ID` | gateway server key or UUID |
| `mcp_tool` | `AO_MCP_DEFAULT_TOOL` | upstream (unprefixed) tool name |
| `mcp_argument` | `AO_MCP_ARGUMENT_NAME` (`query`) | parameter the request text is passed as |
| `mcp_arguments` | `AO_MCP_DEFAULT_ARGUMENTS` | static JSON object merged under the query |
| `mcp_query` | — | fixed argument value instead of the request text |

`capabilities` stay the routing signal, so the planner needs no MCP-specific
knowledge. `protocol: "mcp"` on the card is the only routing switch.

### Why the gateway owns the MCP session

LiteLLM negotiates the protocol version, holds upstream credentials, and handles
streamable HTTP / SSE / stdio reconnects. Delegating that keeps one egress path
for auth, logging, spend and rate limits — the same argument as for A2A.

Two gateway quirks worth knowing:

* tool names in a global list are **prefixed** (`server-tool`); per-server lists
  return the unprefixed name. The adapter uses per-server listing and the
  unprefixed name.
* `arguments: null` is a 500. The adapter always sends an object, `{}` at worst.

### Reference server

`examples/reference_mcp/` is a three-tool FastMCP server used for local testing.
It is not part of the platform. Note it must allow the container hostname as a
`Host` header, or the SDK answers `421 Misdirected Request`.

## Adding REST

Same pattern: map `input_schema` to the HTTP body, map the response into
`InvocationResult.text`/`structured`, surface `Retry-After` as a retry hint.

## Idempotency

The A2A adapter generates an invocation id per attempt but reuses the task id for
legacy retries. Downstream agents should treat `messageId`/task id as the
idempotency key so a retry after a timeout cannot double-apply a write.
