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

### Gateway mode (the only production mode)

`AO_A2A_ROUTING=litellm_proxy` posts the same JSON-RPC envelope to the LiteLLM
agent gateway instead of the agent, adding `x-litellm-api-key`,
`A2A-Version`, `X-LiteLLM-Trace-Id` and `X-LiteLLM-Agent-Id`. The legacy
`tasks/send` fallback is disabled there because the gateway exposes a fixed method
set. See [litellm-a2a.md](litellm-a2a.md).

In this mode the card's `endpoint` is metadata for the gateway's own config (see
`scripts/export_litellm_agents.py`); the orchestrator never dials it.

### What a peer must expose

```
GET  /…/.well-known/agent-card.json
POST /…
{"jsonrpc":"2.0","id":"…","method":"message/send",
 "params":{"message":{"role":"user","messageId":"…","parts":[{"kind":"text","text":"…"}]},
           "metadata":{…},"configuration":{"blocking":true}}}
```

See `examples/mock_a2a_agents/app.py` for a 100-line reference peer.

## Adding MCP

```python
class MCPAdapter(AgentAdapter):
    protocol = "mcp"

    async def invoke(self, card, request):
        # connect to card.endpoint (streamable HTTP), list/filter tools from the
        # card's capability metadata, call the selected tool, normalize the result
        ...

    async def check_health(self, card): ...
    async def discover(self, endpoint): ...  # optional: import tools as capabilities
```

Register in `AdapterRegistry.default()`. Set `protocol: "mcp"` on the card.
Policy, approvals and tracing work unchanged because they operate on the card and
the normalized `InvocationResult`.

## Adding REST

Same pattern: map `input_schema` to the HTTP body, map the response into
`InvocationResult.text`/`structured`, surface `Retry-After` as a retry hint.

## Idempotency

The A2A adapter generates an invocation id per attempt but reuses the task id for
legacy retries. Downstream agents should treat `messageId`/task id as the
idempotency key so a retry after a timeout cannot double-apply a write.
