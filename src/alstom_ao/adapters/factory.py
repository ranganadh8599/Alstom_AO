"""Adapter registry: the only place that maps a protocol to an implementation."""

from __future__ import annotations

import httpx

from alstom_ao.adapters.a2a import A2AAdapter
from alstom_ao.adapters.base import AgentAdapter
from alstom_ao.config import Settings
from alstom_ao.domain.enums import AgentProtocol
from alstom_ao.errors import UnsupportedProtocolError


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, AgentAdapter] = {}

    def register(self, adapter: AgentAdapter) -> None:
        self._adapters[adapter.protocol] = adapter

    def get(self, protocol: str | AgentProtocol) -> AgentAdapter:
        key = str(protocol)
        adapter = self._adapters.get(key)
        if not adapter:
            raise UnsupportedProtocolError(
                f"No adapter is registered for protocol '{key}'.",
                details={"supported": sorted(self._adapters)},
            )
        return adapter

    def supports(self, protocol: str | AgentProtocol) -> bool:
        return str(protocol) in self._adapters

    @property
    def protocols(self) -> list[str]:
        return sorted(self._adapters)

    async def aclose(self) -> None:
        for adapter in self._adapters.values():
            await adapter.aclose()

    @classmethod
    def default(
        cls,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> AdapterRegistry:
        registry = cls()
        registry.register(A2AAdapter(settings, transport=transport))
        # Future: registry.register(MCPAdapter(settings))
        #         registry.register(RestAdapter(settings))
        return registry
