"""A2A (Agent-to-Agent) adapter over JSON-RPC 2.0, via the LiteLLM agent gateway.

Agents are reachable **only** through the gateway: ``POST
{AO_LITELLM_PROXY_URL}/a2a/{agent_name}``, authenticated with a LiteLLM virtual
key. There is no code path that dials an agent endpoint directly, so the gateway
is the single egress point for credentials, logging, spend and protocol
conversion. ``card.endpoint`` is registry metadata (used to generate the gateway
config) and is never contacted by this adapter.

What the adapter implements:

* discovery of the agent card through the gateway
  (``/.well-known/agent-card.json``, then legacy ``/.well-known/agent.json``)
* ``message/send`` JSON-RPC 2.0; the gateway serves a fixed method set, so there
  is no legacy ``tasks/send`` fallback
* propagation of the full trace context and both identities in JSON-RPC
  ``metadata`` *and* in ``X-Alstom-*`` headers
* idempotent task/message ids so a retry never causes a duplicate side effect
  downstream
* bounded retries with exponential backoff for transport/5xx failures
* ``A2A-Version`` pinned to the wire format the gateway was asked to serve, and
  ``X-LiteLLM-Trace-Id``/``X-LiteLLM-Agent-Id`` forwarded so agent-side LLM calls
  group under the same trace

Agent name resolution: ``card.metadata.litellm_agent_name`` when present,
otherwise ``card.agent_id``.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

import httpx

from alstom_ao.adapters.base import AgentAdapter
from alstom_ao.config import Settings
from alstom_ao.domain.enums import AgentHealth, TraceStatus
from alstom_ao.domain.models import (
    AgentCard,
    InvocationRequest,
    InvocationResult,
)
from alstom_ao.errors import AgentInvocationError, AgentTimeoutError
from alstom_ao.security.identity import propagation_headers

logger = logging.getLogger(__name__)

TERMINAL_FAILURES = {"failed", "canceled", "cancelled", "rejected"}
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _text_from_parts(parts: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    for part in parts or []:
        if not isinstance(part, dict):
            continue
        kind = part.get("kind") or part.get("type")
        if kind == "text" and part.get("text"):
            chunks.append(str(part["text"]))
        elif kind == "data" and part.get("data") is not None:
            chunks.append("```json\n" + str(part["data"]) + "\n```")
        elif kind == "file":
            name = (part.get("file") or {}).get("name") or "file"
            chunks.append(f"[file: {name}]")
    return "\n".join(chunk for chunk in chunks if chunk).strip()


def _collect_artifacts(result: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts = result.get("artifacts") or []
    if isinstance(artifacts, list):
        return [artifact for artifact in artifacts if isinstance(artifact, dict)]
    return []


def parse_a2a_result(result: dict[str, Any]) -> tuple[str, list[dict[str, Any]], str, str | None]:
    """Return ``(text, artifacts, state, error)`` from an A2A result object."""

    artifacts = _collect_artifacts(result)
    state = "completed"
    error: str | None = None
    parts: list[dict[str, Any]] = []

    message = result.get("message")
    if isinstance(message, dict):
        parts.extend(message.get("parts") or [])

    status = result.get("status")
    if isinstance(status, dict):
        state = str(status.get("state") or "completed")
        status_message = status.get("message")
        if isinstance(status_message, dict):
            parts.extend(status_message.get("parts") or [])
        if state in TERMINAL_FAILURES and not error:
            error = _text_from_parts((status_message or {}).get("parts") or []) or f"agent reported state '{state}'"

    for artifact in artifacts:
        parts.extend(artifact.get("parts") or [])

    text = _text_from_parts(parts)
    if state in TERMINAL_FAILURES and not text:
        text = ""
    return text, artifacts, state, error


class A2AAdapter(AgentAdapter):
    protocol = "a2a"

    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__(settings)
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    # -- client lifecycle --------------------------------------------------
    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.settings.a2a_timeout_seconds),
                transport=self._transport,
                follow_redirects=True,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- routing -----------------------------------------------------------
    def agent_route_name(self, card: AgentCard) -> str:
        """Name the LiteLLM gateway knows this agent by."""

        name = card.metadata.get("litellm_agent_name") if isinstance(card.metadata, dict) else None
        return str(name or card.agent_id)

    def target_url(self, card: AgentCard) -> str:
        """Gateway JSON-RPC endpoint for this agent. The only way to reach one."""

        return self._proxy_base_for(self.agent_route_name(card))

    def _proxy_base_for(self, name: str) -> str:
        prefix = self.settings.litellm_agent_prefix.rstrip("/")
        return f"{self.settings.litellm_proxy_base}{prefix}/{name}"

    def _gateway_headers(self) -> dict[str, str]:
        key = self.settings.litellm_proxy_key()
        if not key:
            raise AgentInvocationError(
                "Agent calls require a LiteLLM virtual key.",
                details="Set AO_LITELLM_PROXY_API_KEY or AO_LITELLM_PROXY_API_KEY_REF=env:NAME.",
                code="missing_proxy_key",
            )
        # x-litellm-api-key is preferred when the inbound Authorization header may
        # carry a token destined for the backend agent.
        return {"x-litellm-api-key": f"Bearer {key}", "Authorization": f"Bearer {key}"}

    # -- discovery / health ------------------------------------------------
    def _discovery_base(self, endpoint: str) -> str:
        """Map an agent name or URL onto its gateway path.

        Callers pass either the gateway name (``finance``) or the agent's own URL
        from the card; both resolve to ``{gateway}/a2a/{name}``.
        """

        candidate = (endpoint or "").strip().rstrip("/")
        name = candidate.split("/")[-1] if candidate.lower().startswith("http") else candidate
        return self._proxy_base_for(name)

    async def _discover_at(self, base: str) -> dict[str, Any] | None:
        client = self._get_client()
        headers = self._gateway_headers()
        for path in (self.settings.a2a_discovery_path, self.settings.a2a_legacy_discovery_path):
            url = f"{base.rstrip('/')}{path}"
            try:
                response = await client.get(url, headers=headers)
                if response.status_code == 200:
                    body = response.json()
                    if isinstance(body, dict):
                        return body
            except (httpx.HTTPError, ValueError) as exc:
                logger.debug("A2A discovery failed at %s: %s", url, exc)
        return None

    async def discover(self, endpoint: str) -> dict[str, Any] | None:
        return await self._discover_at(self._discovery_base(endpoint))

    async def check_health(self, card: AgentCard) -> AgentHealth:
        document = await self.discover(self.agent_route_name(card))
        if document is None:
            return AgentHealth.UNHEALTHY
        return AgentHealth.HEALTHY if document else AgentHealth.DEGRADED

    # -- invocation --------------------------------------------------------
    async def invoke(self, card: AgentCard, request: InvocationRequest) -> InvocationResult:
        started = time.perf_counter()
        invocation_id = f"{request.trace.run_id}-{card.agent_id}-{uuid.uuid4().hex[:8]}"
        metadata = {
            "trace_context": request.trace.as_metadata(),
            "identity": request.identity.as_metadata(),
            "use_case": request.trace.use_case,
            "agent_identity": {
                "agent_id": card.agent_id,
                "agent_name": card.name,
                "agent_version": card.version,
                "owner": card.owner,
                "protocol": self.protocol,
            },
        }
        trace_map = request.trace.model_dump(mode="json")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            **propagation_headers(trace_map, request.identity),
            **self._gateway_headers(),
            # Wire format the gateway should serve us, and the ids it needs to
            # group agent-side LLM calls under this run.
            "A2A-Version": self.settings.litellm_a2a_protocol_version,
        }
        if self.settings.litellm_forward_trace_headers:
            headers["X-LiteLLM-Trace-Id"] = request.trace.trace_id
            headers["X-LiteLLM-Agent-Id"] = self.agent_route_name(card)
        message_text = request.query if not request.instructions else f"{request.query}\n\nFocus: {request.instructions}"

        target = self.target_url(card)
        last_error: Exception | None = None
        attempts = max(1, self.settings.a2a_max_retries + 1)

        for attempt in range(attempts):
            try:
                payload = self._build_payload(card, message_text, metadata, invocation_id)
                body = await self._post(target, payload, headers)
                result = self._unwrap(body)
                text, artifacts, state, error = parse_a2a_result(result)
                status = TraceStatus.FAILED if state in TERMINAL_FAILURES else TraceStatus.SUCCEEDED
                if status == TraceStatus.SUCCEEDED and not text and not artifacts:
                    status = TraceStatus.FAILED
                    error = error or "agent returned an empty response"
                return InvocationResult(
                    agent_id=card.agent_id,
                    agent_name=card.name,
                    agent_version=card.version,
                    protocol=self.protocol,
                    status=status,
                    text=text,
                    artifacts=artifacts,
                    structured={"state": state, "artifacts": artifacts},
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    retry_count=attempt,
                    error=error,
                    raw=result,
                )
            except httpx.TimeoutException as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    raise AgentTimeoutError(
                        f"Agent '{card.agent_id}' timed out after {self.settings.a2a_timeout_seconds}s."
                    ) from exc
            except (httpx.HTTPError, AgentInvocationError) as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    break
            await asyncio.sleep(min(0.4 * (2**attempt), 3.0))

        raise AgentInvocationError(
            f"Agent '{card.agent_id}' failed after {attempts} attempt(s).",
            details=str(last_error),
        )

    def _build_payload(
        self,
        card: AgentCard,
        message_text: str,
        metadata: dict[str, Any],
        invocation_id: str,
    ) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": f"rpc-{uuid.uuid4().hex[:12]}",
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "messageId": invocation_id,
                    "parts": [{"kind": "text", "text": message_text}],
                },
                "metadata": metadata,
                "configuration": {"blocking": True, "acceptedOutputModes": ["text/plain", "application/json"]},
            },
        }

    async def _post(self, endpoint: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        client = self._get_client()
        response = await client.post(endpoint, json=payload, headers=headers)

        if response.status_code in RETRYABLE_STATUS:
            raise AgentInvocationError(
                f"Agent gateway returned HTTP {response.status_code}.",
                details=response.text[:500],
            )
        if response.status_code >= 400:
            raise AgentInvocationError(
                f"Agent gateway returned HTTP {response.status_code}.",
                details=response.text[:500],
            )
        try:
            return response.json()
        except ValueError as exc:
            raise AgentInvocationError("Agent returned a non-JSON response.") from exc
    @staticmethod
    def _unwrap(body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise AgentInvocationError("Agent returned an unexpected JSON payload.")
        error = body.get("error")
        if error:
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise AgentInvocationError(f"A2A JSON-RPC error: {message}", details=error)
        result = body.get("result")
        if not isinstance(result, dict):
            raise AgentInvocationError("A2A response is missing a 'result' object.")
        return result
