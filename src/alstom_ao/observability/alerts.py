"""Fire-and-forget budget alerts.

Operators need to hear about an 80% crossing *before* traffic is blocked, and an
alert must never be the reason a run fails. Sends are dispatched as background
tasks with a short timeout and every exception is swallowed.

Payload is a generic JSON document; the Slack-compatible ``text`` field is
included so it also works as an incoming-webhook message.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from httpx import AsyncClient

from alstom_ao.config import Settings
from alstom_ao.llm.gateway import BudgetDecision
from alstom_ao.observability.metrics import metrics

logger = logging.getLogger(__name__)

# Keep a strong reference so tasks are not garbage-collected mid-flight.
_pending: set[asyncio.Task] = set()


def build_payload(
    *,
    decision: BudgetDecision,
    run_id: str,
    workflow_id: str,
    user_id: str,
    environment: str,
    model: str,
) -> dict[str, Any]:
    level = "exhausted" if decision.state.value == "exceeded" else "approaching"
    text = (
        f"[{environment}] Budget {level} on run {run_id}: "
        f"{decision.dimension} {decision.used:g}/{decision.limit:g} ({decision.percent:.0%})"
    )
    return {
        "text": text,
        "event": "budget.exceeded" if level == "exhausted" else "budget.warning",
        "run_id": run_id,
        "workflow_id": workflow_id,
        "user_id": user_id,
        "environment": environment,
        "model": model,
        "dimension": decision.dimension,
        "used": decision.used,
        "limit": decision.limit,
        "percent": round(decision.percent, 4),
    }


async def _send(url: str, payload: dict[str, Any], limit_seconds: float) -> None:
    try:
        async with AsyncClient(timeout=limit_seconds) as client:
            response = await client.post(url, json=payload)
            if response.status_code >= 400:
                logger.warning("Budget alert webhook returned HTTP %s", response.status_code)
                metrics.incr("ao.budget.alert_failed")
            else:
                metrics.incr("ao.budget.alert_sent")
    except Exception as exc:  # alerting must never break a run
        logger.warning("Budget alert webhook failed: %s", exc)
        metrics.incr("ao.budget.alert_failed")


def emit_alert(
    settings: Settings,
    *,
    decision: BudgetDecision,
    run_id: str,
    workflow_id: str,
    user_id: str,
    environment: str,
    model: str,
) -> bool:
    """Schedule an alert. Returns False when alerting is not configured."""

    if not settings.budget_alert_webhook:
        return False
    payload = build_payload(
        decision=decision,
        run_id=run_id,
        workflow_id=workflow_id,
        user_id=user_id,
        environment=environment,
        model=model,
    )
    metrics.incr("ao.budget.alert_queued")
    task = asyncio.create_task(
        _send(settings.budget_alert_webhook, payload, settings.budget_alert_timeout_seconds)
    )
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    return True


async def drain(limit_seconds: float = 5.0) -> None:
    """Wait for in-flight alerts; used by tests and graceful shutdown."""

    if not _pending:
        return
    await asyncio.wait(set(_pending), timeout=limit_seconds)
