"""A2A (Agent-to-Agent) adapter over JSON-RPC 2.0.

Implements the parts of the A2A spec the orchestrator needs:

* discovery of the agent card at ``/.well-known/agent-card.json`` (with the
  legacy ``/.well-known/agent.json`` fallback)
* ``message/send`` (A2A 0.2+) with automatic fallback to the legacy
  ``tasks/send`` method
* propagation of the full trace context and both identities in JSON-RPC
  ``metadata`` *and* in ``X-Alstom-*`` headers
* idempotent task/message ids so a retry never causes a duplicate side effect
  downstream
* bounded retries with exponential backoff for transport/5xx failures

Two routing modes (``AO_A2A_ROUTING``)
--------------------------------------
``direct``         Call each agent's own endpoint (``card.endpoint``).
``litellm_proxy``  Call the LiteLLM **agent gateway** instead:
                   ``{AO_LITELLM_PROXY_URL}/a2a/{agent_name}``, authenticating
                   with a LiteLLM virtual key. LiteLLM then handles upstream
                   routing, logging, spend and protocol-version conversion, while
                   this adapter keeps the registry, policy, identity and tracing
                   semantics. The agent name is ``card.metadata.litellm_agent_name``
                   when present, otherwise ``card.agent_id``.

The wire format is unchanged in both modes: JSON-RPC 2.0 ``message/send``. In
proxy mode the ``A2A-Version`` header carries the protocol version the proxy was
asked to serve (``0.3`` or ``1.0``), and ``X-LiteLLM-Trace-Id`` is forwarded so
agent-side LLM calls group under the same trace.
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
from alstom_ao.security.identity import propagation_headers, resolve_secret

logger = logging.getLogger(__name__)

A2A_VERSION = "0.2.5"
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
    @property
    def via_litellm_proxy(self) -> bool:
        return self.settings.uses_litellm_proxy

    def agent_route_name(self, card: AgentCard) -> str:
        """Name the LiteLLM gateway knows this agent by."""

        name = card.metadata.get("litellm_agent_name") if isinstance(card.metadata, dict) else None
        return str(name or card.agent_id)

    def target_url(self, card: AgentCard) -> str:
        """JSON-RPC endpoint for this agent under the active routing mode."""

        if self.via_litellm_proxy:
            prefix = self.settings.litellm_agent_prefix.rstrip("/")
            return f"{self.settings.litellm_proxy_base}{prefix}/{self.agent_route_name(card)}"
        return card.endpoint

    def _proxy_base_for(self, name: str) -> str:
        prefix = self.settings.litellm_agent_prefix.rstrip("/")
        return f"{self.settings.litellm_proxy_base}{prefix}/{name}"

    def _proxy_headers(self) -> dict[str, str]:
        key = self.settings.litellm_proxy_key()
        if not key:
            raise AgentInvocationError(
                "AO_A2A_ROUTING=litellm_proxy requires a LiteLLM virtual key.",
                details="Set AO_LITELLM_PROXY_API_KEY or AO_LITELLM_PROXY_API_KEY_REF=env:NAME.",
                code="missing_proxy_key",
            )
        # x-litellm-api-key is preferred when the inbound Authorization header may
        # carry a token destined for the backend agent.
        return {"x-litellm-api-key": f"Bearer {key}", "Authorization": f"Bearer {key}"}

    # -- auth --------------------------------------------------------------
    def _auth_headers(self, card: AgentCard) -> dict[str, str]:
        if self.via_litellm_proxy:
            # The gateway holds the agent's own credentials; we authenticate to it.
            return self._proxy_headers()

        auth = card.auth
        if auth.type == "none" or not auth.secret_ref:
            return {}
        secret = resolve_secret(auth.secret_ref, self.settings)
        if not secret:
            raise AgentInvocationError(
                f"Agent '{card.agent_id}' requires {auth.type} auth but secret '{auth.secret_ref}' is not resolvable.",
                details="Set the referenced environment variable.",
            )
        if auth.type == "bearer":
            return {"Authorization": f"Bearer {secret}"}
        if auth.type == "api_key":
            return {auth.header_name or "X-API-Key": secret}
        raise AgentInvocationError(
            f"Auth type '{auth.type}' is not implemented by the A2A adapter yet.",
            code="unsupported_auth",
        )

    # -- discovery / health ------------------------------------------------
    def _discovery_bases(self, endpoint: str) -> list[str]:
        """Base URLs to probe for an agent card.

        In proxy mode an agent name (``finance``) or the agent's own URL is mapped
        onto the gateway path, so import works against the gateway too.
        """

        if self.via_litellm_proxy:
            candidate = endpoint.strip().rstrip("/")
            if candidate.lower().startswith("http"):
                name = candidate.split("/")[-1]
            else:
                name = candidate
            return [self._proxy_base_for(name)]
        return [endpoint.rstrip("/")]

    def _discovery_headers(self) -> dict[str, str]:
        return self._proxy_headers() if self.via_litellm_proxy else {}

    async def _discover_at(self, base: str, headers: dict[str, str] | None = None) -> dict[str, Any] | None:
        client = self._get_client()
        for path in (self.settings.a2a_discovery_path, self.settings.a2a_legacy_discovery_path):
            url = f"{base.rstrip('/')}{path}"
            try:
                response = await client.get(url, headers=headers or None)
                if response.status_code == 200:
                    body = response.json()
                    if isinstance(body, dict):
                        return body
            except (httpx.HTTPError, ValueError) as exc:
                logger.debug("A2A discovery failed at %s: %s", url, exc)
        return None

    async def discover(self, endpoint: str) -> dict[str, Any] | None:
        headers = self._discovery_headers()
        for base in self._discovery_bases(endpoint):
            document = await self._discover_at(base, headers)
            if document is not None:
                return document
        return None

    async def check_health(self, card: AgentCard) -> AgentHealth:
        document = await self.discover(self.target_url(card))
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
            **self._auth_headers(card),
        }
        if self.via_litellm_proxy:
            # Tell the gateway which wire format to serve, and give it the trace
            # ids it needs to group agent-side LLM calls under our run.
            headers["A2A-Version"] = self.settings.litellm_a2a_protocol_version
            if self.settings.litellm_forward_trace_headers:
                headers["X-LiteLLM-Trace-Id"] = request.trace.trace_id
                headers["X-LiteLLM-Agent-Id"] = self.agent_route_name(card)
        else:
            headers["A2A-Version"] = A2A_VERSION
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
                f"Agent endpoint returned HTTP {response.status_code}.",
                details=response.text[:500],
            )
        if response.status_code in {404, 405, 501} and not self.via_litellm_proxy:
            # Older *direct* A2A peers only expose ``tasks/send``. The LiteLLM
            # gateway serves a fixed method set, so retrying with a legacy method
            # there would only mask the real routing error.
            payload["method"] = "tasks/send"
            payload["params"] = {
                "id": payload["params"]["message"]["messageId"],
                "message": payload["params"]["message"],
                "metadata": payload["params"]["metadata"],
            }
            response = await client.post(endpoint, json=payload, headers=headers)
            if response.status_code >= 500:
                raise AgentInvocationError(f"Agent endpoint returned HTTP {response.status_code}.")

        if response.status_code >= 400:
            raise AgentInvocationError(
                f"Agent endpoint returned HTTP {response.status_code}.",
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
