"""Adapter contract.

Adding a protocol later (MCP, REST, gRPC) means implementing this interface and
registering it — no orchestrator change. The orchestrator only knows
``AgentAdapter.invoke()``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from alstom_ao.config import Settings
from alstom_ao.domain.enums import AgentHealth
from alstom_ao.domain.models import AgentCard, InvocationRequest, InvocationResult


class AgentAdapter(ABC):
    protocol: str = "unknown"
    supports_streaming: bool = False

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @abstractmethod
    async def invoke(self, card: AgentCard, request: InvocationRequest) -> InvocationResult:
        """Call one agent and normalize the response into an InvocationResult."""

    @abstractmethod
    async def check_health(self, card: AgentCard) -> AgentHealth:
        """Cheap reachability probe used by the registry."""

    @abstractmethod
    async def discover(self, endpoint: str) -> dict[str, Any] | None:
        """Fetch a protocol-native capability document (A2A agent card today)."""

    async def aclose(self) -> None:  # pragma: no cover - optional hook
        return None
