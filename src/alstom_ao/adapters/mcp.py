"""MCP (Model Context Protocol) adapter, via the LiteLLM MCP gateway.

MCP servers publish **tools**, not conversational agents. This adapter therefore
maps one registry card onto one tool on one server, and calls it through the
gateway's REST surface rather than speaking MCP itself:

    POST {AO_LITELLM_PROXY_URL}/mcp-rest/tools/call
    {"server_id": "<server>", "name": "<tool>", "arguments": {...}}

Why the gateway and not MCP directly:

* LiteLLM owns the MCP session — ``initialize``, protocol-version negotiation,
  transport (streamable HTTP / SSE / stdio) and reconnects.
* Upstream MCP credentials live in the gateway (``auth_type``, OAuth, SigV4),
  never in this service or in a card.
* One egress path, so spend, logging and rate limits stay in one place — the same
  reason the A2A adapter routes through the gateway.

Card mapping (all optional, with sensible fallbacks):

| Value            | Source                                                        |
|-----------------|---------------------------------------------------------------|
| ``server_id``    | ``card.metadata.mcp_server_id`` → ``AO_MCP_DEFAULT_SERVER_ID`` |
| tool name        | ``card.metadata.mcp_tool`` → ``AO_MCP_DEFAULT_TOOL``           |
| static arguments | ``card.metadata.mcp_arguments`` → ``AO_MCP_DEFAULT_ARGUMENTS`` |
| query parameter  | ``card.metadata.mcp_argument`` → ``AO_MCP_ARGUMENT_NAME``      |

The card's ``capabilities`` stay the routing signal, exactly as for A2A agents,
so the planner needs no MCP-specific knowledge.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx

from alstom_ao.adapters.base import AgentAdapter
from alstom_ao.config import Settings
from alstom_ao.domain.enums import AgentHealth, TraceStatus
from alstom_ao.domain.models import AgentCard, InvocationRequest, InvocationResult
from alstom_ao.errors import AgentInvocationError, AgentTimeoutError, InputValidationError
from alstom_ao.security.identity import propagation_headers

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def render_tool_result(payload: Any) -> str:
    """Flatten an MCP ``tools/call`` result into readable text.

    MCP returns ``{"content": [{"type": "text", "text": "..."}], "isError": bool}``
    but LiteLLM may hand back the raw upstream object, a plain string, or a JSON
    document from a ``structuredContent`` tool. Handle all three so the
    synthesizer always receives prose.
    """

    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload.strip()
    if not isinstance(payload, dict):
        return json.dumps(payload, ensure_ascii=False)

    # LiteLLM wraps gateway failures as {"detail": {"error": ..., "message": ...}}.
    detail = payload.get("detail")
    if isinstance(detail, dict):
        code = detail.get("error") or "mcp_error"
        message = detail.get("message") or ""
        return f"{code}: {message}".strip(": ")

    # LiteLLM sometimes wraps the MCP result in a result envelope.
    for key in ("result", "data", "output"):
        inner = payload.get(key)
        if isinstance(inner, (dict, list, str)):
            rendered = render_tool_result(inner)
            if rendered:
                return rendered

    chunks: list[str] = []
    content = payload.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            kind = item.get("type") or item.get("kind")
            if kind == "text" and item.get("text"):
                chunks.append(str(item["text"]))
            elif kind in {"json", "data"} and item.get("data") is not None:
                chunks.append(json.dumps(item["data"], ensure_ascii=False, indent=2))
            elif kind == "resource" and item.get("resource"):
                resource = item["resource"]
                text = (resource or {}).get("text")
                chunks.append(str(text) if text else f"[resource: {(resource or {}).get('uri', 'unknown')}]")
            elif kind == "image":
                chunks.append("[image content omitted]")
        if chunks:
            return "\n".join(chunks).strip()
        # An explicit empty content list means the tool produced nothing.
        return ""

    structured = payload.get("structuredContent")
    if structured is not None:
        # Single-key {"result": "..."} is the common shape; unwrap it.
        if isinstance(structured, dict) and len(structured) == 1 and isinstance(structured.get("result"), str):
            return structured["result"].strip()
        return json.dumps(structured, ensure_ascii=False, indent=2)

    # Unknown shape: never drop data, just show it.
    return json.dumps(payload, ensure_ascii=False)


def parse_arguments(raw: str | dict[str, Any] | None) -> dict[str, Any]:
    """Accept the static-argument setting as a dict or a JSON object string."""

    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise InputValidationError(
            "mcp_arguments must be a JSON object.",
            details=str(exc),
        ) from exc
    if not isinstance(parsed, dict):
        raise InputValidationError("mcp_arguments must be a JSON object.")
    return parsed


class MCPAdapter(AgentAdapter):
    protocol = "mcp"

    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__(settings)
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    # -- client lifecycle --------------------------------------------------
    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.settings.mcp_timeout_seconds),
                transport=self._transport,
                follow_redirects=True,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- card mapping ------------------------------------------------------
    def _meta(self, card: AgentCard) -> dict[str, Any]:
        return card.metadata if isinstance(card.metadata, dict) else {}

    def server_id(self, card: AgentCard) -> str:
        value = self._meta(card).get("mcp_server_id") or self.settings.mcp_default_server_id
        if not value:
            raise AgentInvocationError(
                f"MCP agent '{card.agent_id}' has no server_id.",
                details="Set metadata.mcp_server_id on the card, or AO_MCP_DEFAULT_SERVER_ID.",
                code="missing_mcp_server",
            )
        return str(value)

    def tool_name(self, card: AgentCard) -> str:
        value = self._meta(card).get("mcp_tool") or self.settings.mcp_default_tool
        if not value:
            raise AgentInvocationError(
                f"MCP agent '{card.agent_id}' has no tool name.",
                details="Set metadata.mcp_tool on the card, or AO_MCP_DEFAULT_TOOL.",
                code="missing_mcp_tool",
            )
        return str(value)

    def argument_name(self, card: AgentCard) -> str:
        return str(self._meta(card).get("mcp_argument") or self.settings.mcp_argument_name)

    def _static_arguments(self, card: AgentCard) -> dict[str, Any]:
        raw = self._meta(card).get("mcp_arguments")
        if raw is None:
            return parse_arguments(self.settings.mcp_default_arguments)
        return parse_arguments(raw)

    def _build_arguments(self, card: AgentCard, query: str, instructions: str | None) -> dict[str, Any]:
        """Build the tool arguments.

        The planner's ``instructions`` are guidance for a *conversational* agent and
        are deliberately **not** appended here: an MCP tool parameter is typed data,
        so injecting prose corrupts the argument and can silently mismatch a record.
        Use ``metadata.mcp_arguments`` for anything extra the tool needs.
        """

        explicit = self._meta(card).get("mcp_query")
        arguments = self._static_arguments(card)
        arguments[self.argument_name(card)] = explicit if explicit else query.strip()
        return arguments

    # -- gateway plumbing --------------------------------------------------
    def _gateway_headers(self) -> dict[str, str]:
        key = self.settings.litellm_proxy_key()
        if not key:
            raise AgentInvocationError(
                "MCP tool calls require a LiteLLM virtual key.",
                details="Set AO_LITELLM_PROXY_API_KEY or AO_LITELLM_PROXY_API_KEY_REF=env:NAME.",
                code="missing_proxy_key",
            )
        # MCP traffic prefers x-litellm-api-key so Authorization stays free for
        # upstream OAuth tokens the gateway may need to forward.
        return {"x-litellm-api-key": f"Bearer {key}"}

    def _url(self, path: str) -> str:
        return f"{self.settings.litellm_proxy_base}{path}"

    # -- discovery / health ------------------------------------------------
    async def discover(self, endpoint: str) -> dict[str, Any] | None:
        """Return the tool descriptor for ``endpoint``.

        ``endpoint`` is either ``server_id``, ``server_id/tool``, or a plain tool
        name. Tools are listed through the gateway, so the answer is what the
        gateway can actually reach — not what the card claims.
        """

        raw = (endpoint or "").strip().strip("/")
        if not raw:
            return None
        if "/" in raw:
            server, tool = raw.split("/", 1)
        else:
            server, tool = self.settings.mcp_default_server_id, raw

        tools = await self.list_tools(server)
        if tools is None:
            return None
        for item in tools:
            if str(item.get("name")) == tool:
                return item
        return None

    async def list_tools(self, server_id: str | None = None) -> list[dict[str, Any]] | None:
        """List tools the gateway exposes, optionally narrowed to one server."""

        params = {"server_id": server_id} if server_id else None
        try:
            response = await self._get_client().get(
                self._url(self.settings.mcp_tools_list_path),
                params=params,
                headers={**self._gateway_headers(), "Accept": "application/json"},
            )
        except (httpx.HTTPError, AgentInvocationError) as exc:
            logger.debug("MCP tools/list failed: %s", exc)
            return None
        if response.status_code != 200:
            logger.debug("MCP tools/list returned %s: %s", response.status_code, response.text[:200])
            return None
        try:
            body = response.json()
        except ValueError:
            return None
        tools = body.get("tools") if isinstance(body, dict) else None
        return [tool for tool in tools or [] if isinstance(tool, dict)]

    async def check_health(self, card: AgentCard) -> AgentHealth:
        try:
            server = self.server_id(card)
            tool = self.tool_name(card)
        except AgentInvocationError:
            return AgentHealth.UNHEALTHY
        tools = await self.list_tools(server)
        if tools is None:
            return AgentHealth.UNHEALTHY
        return AgentHealth.HEALTHY if any(str(item.get("name")) == tool for item in tools) else AgentHealth.DEGRADED

    # -- invocation --------------------------------------------------------
    async def invoke(self, card: AgentCard, request: InvocationRequest) -> InvocationResult:
        started = time.perf_counter()
        server = self.server_id(card)
        tool = self.tool_name(card)
        arguments = self._build_arguments(card, request.query, request.instructions)

        trace_map = request.trace.model_dump(mode="json")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            **propagation_headers(trace_map, request.identity),
            **self._gateway_headers(),
        }
        if self.settings.litellm_forward_trace_headers:
            headers["X-LiteLLM-Trace-Id"] = request.trace.trace_id
        if self.settings.litellm_forward_trace_headers:
            headers["X-LiteLLM-Agent-Id"] = tool

        payload = {"server_id": server, "name": tool, "arguments": arguments}
        target = self._url(self.settings.mcp_tool_call_path)
        attempts = max(1, self.settings.mcp_max_retries + 1)
        last_error: Exception | None = None

        for attempt in range(attempts):
            try:
                response = await self._get_client().post(target, json=payload, headers=headers)
            except httpx.TimeoutException as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    raise AgentTimeoutError(
                        f"MCP tool '{tool}' timed out after {self.settings.mcp_timeout_seconds}s.",
                        details=f"server_id={server}",
                    ) from exc
                await asyncio.sleep(min(0.4 * (2**attempt), 3.0))
                continue
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    break
                await asyncio.sleep(min(0.4 * (2**attempt), 3.0))
                continue

            if response.status_code in RETRYABLE_STATUS and attempt + 1 < attempts:
                last_error = AgentInvocationError(
                    f"MCP gateway returned {response.status_code}",
                    details=response.text[:300],
                )
                await asyncio.sleep(min(0.4 * (2**attempt), 3.0))
                continue

            return self._to_result(card, response, tool, server, started, attempt)

        raise AgentInvocationError(
            f"MCP tool '{tool}' on server '{server}' failed after {attempts} attempt(s).",
            details=str(last_error),
            code="mcp_invocation_failed",
        )

    def _to_result(
        self,
        card: AgentCard,
        response: httpx.Response,
        tool: str,
        server: str,
        started: float,
        attempt: int,
    ) -> InvocationResult:
        latency_ms = int((time.perf_counter() - started) * 1000)
        raw: Any
        try:
            raw = response.json()
        except ValueError:
            raw = response.text

        error: str | None = None
        text = ""

        if response.status_code != 200:
            detail = raw.get("detail") if isinstance(raw, dict) else None
            if isinstance(detail, dict):
                error = f"{detail.get('error', 'mcp_error')}: {detail.get('message', '')}".strip()
            else:
                error = f"MCP gateway returned {response.status_code}"
            # A tool-level error is a real answer from a working server, so keep
            # the body visible in the trace rather than discarding it.
            text = render_tool_result(raw)
        else:
            text = render_tool_result(raw)
            is_error = bool(isinstance(raw, dict) and raw.get("isError"))
            if is_error:
                error = text or "MCP tool reported an error"
                text = ""

        status = TraceStatus.FAILED if error else TraceStatus.SUCCEEDED
        if status == TraceStatus.SUCCEEDED and not text.strip():
            status = TraceStatus.FAILED
            error = error or "MCP tool returned an empty result"

        return InvocationResult(
            agent_id=card.agent_id,
            agent_name=card.name,
            agent_version=card.version,
            protocol=self.protocol,
            status=status,
            text=text,
            artifacts=[],
            structured={
                "server_id": server,
                "tool": tool,
                "is_error": bool(isinstance(raw, dict) and raw.get("isError")),
                "http_status": response.status_code,
            },
            latency_ms=latency_ms,
            retry_count=attempt,
            error=error,
            raw=raw if isinstance(raw, dict) else {"body": raw},
        )
