"""LiteLLM-backed model gateway with per-run budgets.

The orchestration layer never talks to a provider directly: every LLM call goes
through this gateway, which gives us one place for routing, fallbacks, retries,
timeouts, token accounting and cost limits (the LoomVault lesson: a crash loop
must not be able to outspend a declared budget).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from alstom_ao.config import Settings
from alstom_ao.errors import BudgetExceededError, UpstreamUnavailableError

logger = logging.getLogger(__name__)


@dataclass
class LLMResult:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    finish_reason: str | None = None
    raw_usage: dict = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LLMBudget:
    """Per-run budget. Charges are monotonic; exceeding a limit raises."""

    def __init__(
        self,
        *,
        max_calls: int,
        max_tokens: int,
        max_cost_usd: float,
    ) -> None:
        self.max_calls = max_calls
        self.max_tokens = max_tokens
        self.max_cost_usd = max_cost_usd
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cost_usd = 0.0
        self.models: dict[str, int] = {}

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def ensure_can_call(self) -> None:
        if self.calls >= self.max_calls:
            raise BudgetExceededError(f"Run exceeded the LLM call budget ({self.max_calls}).")
        if self.total_tokens >= self.max_tokens:
            raise BudgetExceededError(f"Run exceeded the token budget ({self.max_tokens}).")
        if self.cost_usd >= self.max_cost_usd:
            raise BudgetExceededError(f"Run exceeded the cost budget (${self.max_cost_usd:.2f}).")

    def charge(self, result: LLMResult) -> None:
        self.calls += 1
        self.prompt_tokens += result.prompt_tokens
        self.completion_tokens += result.completion_tokens
        self.cost_usd += result.cost_usd
        self.models[result.model] = self.models.get(result.model, 0) + 1

    def snapshot(self) -> dict:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "models": dict(self.models),
            "limits": {
                "max_calls": self.max_calls,
                "max_tokens": self.max_tokens,
                "max_cost_usd": self.max_cost_usd,
            },
        }

    @classmethod
    def from_snapshot(cls, snapshot: dict, settings: Settings) -> LLMBudget:
        budget = cls(
            max_calls=settings.llm_max_calls_per_run,
            max_tokens=settings.llm_max_tokens_per_run,
            max_cost_usd=settings.llm_max_cost_usd_per_run,
        )
        budget.calls = int(snapshot.get("calls", 0))
        budget.prompt_tokens = int(snapshot.get("prompt_tokens", 0))
        budget.completion_tokens = int(snapshot.get("completion_tokens", 0))
        budget.cost_usd = float(snapshot.get("cost_usd", 0.0))
        budget.models = dict(snapshot.get("models", {}))
        return budget


class ModelGateway(ABC):
    is_mock: bool = False

    def __init__(self, settings: Settings, budget: LLMBudget) -> None:
        self.settings = settings
        self.budget = budget

    @abstractmethod
    async def complete_text(
        self,
        *,
        system: str,
        user: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult: ...

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> tuple[dict, LLMResult]:
        result = await self.complete_text(
            system=system, user=user, temperature=temperature, max_tokens=max_tokens
        )
        return parse_json_object(result.text), result


class MockGateway(ModelGateway):
    """Deterministic offline gateway.

    Planning still works because the planner uses the heuristic selector when the
    gateway is a mock; this class only needs to produce stable prose so the demo
    and the tests are hermetic.
    """

    is_mock = True

    async def complete_text(
        self,
        *,
        system: str,
        user: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        started = time.perf_counter()
        self.budget.ensure_can_call()
        text = (
            "Mock LLM gateway is enabled. Set AO_LLM_MODE=litellm and configure a model "
            "to generate synthesized answers."
        )
        result = LLMResult(text=text, model="mock/echo", latency_ms=int((time.perf_counter() - started) * 1000))
        self.budget.charge(result)
        return result


class LiteLLMGateway(ModelGateway):
    """Provider-agnostic gateway. Supports OpenAI, Anthropic, Azure, Ollama, vLLM..."""

    is_mock = False

    async def complete_text(
        self,
        *,
        system: str,
        user: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        self.budget.ensure_can_call()
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        last_error: Exception | None = None
        attempts = max(1, self.settings.llm_max_retries + 1)
        for attempt in range(attempts):
            started = time.perf_counter()
            try:
                response = await self._acompletion(messages, temperature=temperature, max_tokens=max_tokens)
                result = self._to_result(response, started)
                self.budget.charge(result)
                return result
            except BudgetExceededError:
                raise
            except Exception as exc:  # provider errors are heterogeneous
                last_error = exc
                logger.warning("LLM call failed (attempt %s/%s): %s", attempt + 1, attempts, exc)
                if attempt + 1 < attempts:
                    await asyncio.sleep(min(0.5 * (2**attempt), 4.0))
        raise UpstreamUnavailableError(
            "The LLM provider is unavailable after retries.",
            details=str(last_error),
        ) from last_error

    async def _acompletion(self, messages: list[dict], *, temperature: float | None, max_tokens: int | None):
        import litellm  # imported lazily: keeps startup fast and tests hermetic

        kwargs: dict = {
            "model": self.settings.llm_model,
            "messages": messages,
            "temperature": self.settings.llm_temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.settings.llm_max_tokens,
            "timeout": self.settings.llm_timeout_seconds,
            "num_retries": 0,
            "drop_params": True,
        }
        if self.settings.llm_api_base:
            kwargs["api_base"] = self.settings.llm_api_base
        if self.settings.llm_api_key:
            kwargs["api_key"] = self.settings.llm_api_key
        if self.settings.fallback_models:
            kwargs["fallbacks"] = self.settings.fallback_models
        return await litellm.acompletion(**kwargs)

    def _to_result(self, response, started: float) -> LLMResult:
        choice = response.choices[0]
        text = choice.message.content or ""
        usage = getattr(response, "usage", None)
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        model = getattr(response, "model", None) or self.settings.llm_model
        cost = 0.0
        try:
            cost = float(response._hidden_params.get("response_cost") or 0.0)
        except Exception:
            try:
                import litellm

                cost = float(litellm.completion_cost(completion_response=response) or 0.0)
            except Exception:
                cost = 0.0
        return LLMResult(
            text=text,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
            latency_ms=int((time.perf_counter() - started) * 1000),
            finish_reason=getattr(choice, "finish_reason", None),
            raw_usage={
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        )


class ScriptedGateway(ModelGateway):
    """Test double: returns queued responses and preserves usage accounting."""

    def __init__(self, settings: Settings, budget: LLMBudget, responses: list[str] | None = None) -> None:
        super().__init__(settings, budget)
        self.responses = list(responses or [])
        self.calls: list[dict] = []

    async def complete_text(
        self,
        *,
        system: str,
        user: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        self.budget.ensure_can_call()
        self.calls.append({"system": system, "user": user})
        text = self.responses.pop(0) if self.responses else "{}"
        result = LLMResult(text=text, model="scripted/test", prompt_tokens=10, completion_tokens=10, cost_usd=0.0)
        self.budget.charge(result)
        return result


def parse_json_object(text: str) -> dict:
    """Tolerant JSON extraction: handles fenced blocks and leading prose."""

    if not text:
        return {}
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    try:
        value = json.loads(cleaned)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            value = json.loads(cleaned[start : end + 1])
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def build_gateway(settings: Settings, budget: LLMBudget, *, responses: list[str] | None = None) -> ModelGateway:
    if settings.llm_mode == "litellm":
        return LiteLLMGateway(settings, budget)
    return ScriptedGateway(settings, budget, responses=responses) if responses is not None else MockGateway(settings, budget)
