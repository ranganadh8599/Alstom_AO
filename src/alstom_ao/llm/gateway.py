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
from enum import StrEnum

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


class BudgetState(StrEnum):
    OK = "ok"
    WARN = "warn"
    EXCEEDED = "exceeded"


@dataclass
class BudgetDecision:
    """Outcome of evaluating a budget at a decision point.

    Tiered semantics (deliberately not loomvault's four-strategy model):

    * ``OK``       nothing crossed
    * ``WARN``     soft threshold (default 80%) crossed: alert, optionally
                   downgrade the model, but keep running
    * ``EXCEEDED`` hard limit crossed: block **new** work at the root, let
                   in-flight agent calls finish so state is never half-broken
    """

    state: BudgetState = BudgetState.OK
    dimension: str | None = None
    used: float = 0.0
    limit: float = 0.0
    percent: float = 0.0

    @property
    def can_start_new_work(self) -> bool:
        return self.state != BudgetState.EXCEEDED

    @property
    def should_degrade(self) -> bool:
        return self.state == BudgetState.WARN

    def describe(self) -> str:
        if self.state == BudgetState.OK:
            return "within budget"
        return f"{self.dimension} {self.used:g}/{self.limit:g} ({self.percent:.0%})"


class LLMBudget:
    """Per-run budget with tiered evaluation.

    ``ensure_can_call`` is the *root entry* gate and raises on a hard limit.
    ``evaluate`` is the *decision point* used mid-run, which never raises.
    """

    def __init__(
        self,
        *,
        max_calls: int,
        max_tokens: int,
        max_cost_usd: float,
        max_hops: int = 8,
        soft_threshold: float = 0.8,
    ) -> None:
        self.max_calls = max_calls
        self.max_tokens = max_tokens
        self.max_cost_usd = max_cost_usd
        self.max_hops = max_hops
        self.soft_threshold = min(max(soft_threshold, 0.1), 1.0)
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cost_usd = 0.0
        self.models: dict[str, int] = {}
        self.hops = 0
        self.warned: set[str] = set()

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    # -- evaluation --------------------------------------------------------
    def _dimensions(self) -> list[tuple[str, float, float]]:
        """(name, used, limit) for every constrained dimension."""

        dims: list[tuple[str, float, float]] = []
        if self.max_calls > 0:
            dims.append(("llm_calls", float(self.calls), float(self.max_calls)))
        if self.max_tokens > 0:
            dims.append(("tokens", float(self.total_tokens), float(self.max_tokens)))
        if self.max_cost_usd > 0:
            dims.append(("cost_usd", float(self.cost_usd), float(self.max_cost_usd)))
        if self.max_hops > 0:
            dims.append(("agent_hops", float(self.hops), float(self.max_hops)))
        return dims

    def evaluate(self) -> BudgetDecision:
        """Hard limits first, then soft thresholds. Pure, never raises."""

        dims = self._dimensions()
        for name, used, limit in dims:
            if limit > 0 and used >= limit:
                return BudgetDecision(
                    state=BudgetState.EXCEEDED,
                    dimension=name,
                    used=used,
                    limit=limit,
                    percent=used / limit,
                )
        for name, used, limit in dims:
            if limit > 0 and used / limit >= self.soft_threshold:
                return BudgetDecision(
                    state=BudgetState.WARN,
                    dimension=name,
                    used=used,
                    limit=limit,
                    percent=used / limit,
                )
        return BudgetDecision()

    def claim_warning(self, decision: BudgetDecision) -> bool:
        """One alert per dimension per run, not per token."""

        key = decision.dimension or "unknown"
        if key in self.warned:
            return False
        self.warned.add(key)
        return True

    def ensure_can_call(self) -> None:
        decision = self.evaluate()
        if decision.state == BudgetState.EXCEEDED:
            raise BudgetExceededError(
                f"Run exceeded its {decision.dimension} budget ({decision.used:g}/{decision.limit:g})."
            )

    def record_hop(self) -> None:
        self.hops += 1

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
            "hops": self.hops,
            "models": dict(self.models),
            "limits": {
                "max_calls": self.max_calls,
                "max_tokens": self.max_tokens,
                "max_cost_usd": self.max_cost_usd,
                "max_hops": self.max_hops,
                "soft_threshold": self.soft_threshold,
            },
        }

    @classmethod
    def from_snapshot(cls, snapshot: dict, settings: Settings) -> LLMBudget:
        budget = cls(
            max_calls=settings.llm_max_calls_per_run,
            max_tokens=settings.llm_max_tokens_per_run,
            max_cost_usd=settings.llm_max_cost_usd_per_run,
            max_hops=settings.max_agent_hops,
            soft_threshold=settings.budget_soft_threshold,
        )
        budget.calls = int(snapshot.get("calls", 0))
        budget.prompt_tokens = int(snapshot.get("prompt_tokens", 0))
        budget.completion_tokens = int(snapshot.get("completion_tokens", 0))
        budget.cost_usd = float(snapshot.get("cost_usd", 0.0))
        budget.hops = int(snapshot.get("hops", 0))
        budget.models = dict(snapshot.get("models", {}))
        return budget


class ModelGateway(ABC):
    def __init__(self, settings: Settings, budget: LLMBudget) -> None:
        self.settings = settings
        self.budget = budget
        self.model = settings.llm_model

    def current_model(self) -> str:
        return self.model

    def downgrade(self, model: str | None) -> bool:
        """Switch to a cheaper model for the rest of the run. False when unsupported."""

        if not model or model == self.model:
            return False
        self.model = model
        return True

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


class LiteLLMGateway(ModelGateway):
    """Provider-agnostic gateway. Supports OpenAI, Anthropic, Azure, Ollama, vLLM..."""

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
            "model": self.model,
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
        model = getattr(response, "model", None) or self.model
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


def build_gateway(settings: Settings, budget: LLMBudget) -> ModelGateway:
    return LiteLLMGateway(settings, budget)
