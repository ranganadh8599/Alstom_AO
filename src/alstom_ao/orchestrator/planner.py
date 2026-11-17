"""Dynamic agent selection.

Two engines, deliberately combined:

* **LLM planner** - understands intent, multi-agent composition, and whether the
  request is consequential enough to need a human.
* **Deterministic heuristic** - never fails, needs no provider, and is used as
  the fallback when the model is unavailable, returns garbage, or picks agents
  that are not in the registry.

The result is a validated ``Plan`` that can only reference registered agents.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from alstom_ao.config import Settings
from alstom_ao.domain.enums import ActionType
from alstom_ao.domain.models import Plan, PlannedAgent
from alstom_ao.llm.gateway import ModelGateway
from alstom_ao.llm.prompts import PLANNER_SYSTEM, build_planner_user_prompt

logger = logging.getLogger(__name__)

STOPWORDS = {
    "the", "and", "for", "with", "what", "which", "who", "how", "why", "when", "where", "can", "could",
    "should", "would", "you", "your", "our", "their", "this", "that", "these", "those", "from", "into",
    "about", "does", "did", "are", "was", "were", "have", "has", "had", "will", "shall", "may", "might",
    "please", "help", "need", "want", "give", "tell", "show", "get", "any", "all", "some", "there",
    "than", "then", "them", "they", "its", "it's", "i", "me", "my", "we", "us", "is", "of", "to", "in",
    "on", "at", "by", "as", "or", "be", "do", "not", "no", "if", "so", "up", "out", "per", "vs",
}

SYNONYMS: dict[str, set[str]] = {
    "invoice": {"billing", "ap", "payable", "payment", "vendor", "supplier"},
    "payment": {"invoice", "billing", "remit"},
    "duplicate": {"double", "twice", "repeat"},
    "hr": {"people", "employee", "leave", "benefit", "policy"},
    "leave": {"holiday", "vacation", "absence", "pto"},
    "vpn": {"network", "remote", "access"},
    "laptop": {"computer", "device", "hardware"},
    "policy": {"rule", "guideline", "standard"},
    "travel": {"flight", "trip", "expense"},
    "access": {"permission", "login", "credential", "account"},
    "purchase": {"po", "procurement", "order"},
    "software": {"application", "app", "license"},
    "ticket": {"incident", "request", "servicenow"},
}

ACTION_KEYWORDS: list[tuple[ActionType, set[str]]] = [
    (ActionType.DELETE, {"delete", "remove", "cancel", "terminate", "close", "purge"}),
    (ActionType.PAYMENT, {"pay", "payment", "reimburse", "transfer", "refund"}),
    (ActionType.EXECUTE, {"run", "execute", "trigger", "deploy", "reset", "restart"}),
    (ActionType.SEND, {"send", "email", "notify", "post", "publish", "reply"}),
    (ActionType.UPDATE, {"update", "change", "modify", "edit", "amend"}),
    (ActionType.WRITE, {"create", "add", "open", "submit", "file", "raise"}),
    (ActionType.ANALYZE, {"analyze", "compare", "audit", "check", "review", "reconcile", "validate"}),
    (ActionType.READ, {"what", "which", "status", "lookup", "find", "list", "show"}),
]


def tokenize(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return [word for word in words if len(word) > 2 and word not in STOPWORDS]


def expand(tokens: set[str]) -> set[str]:
    expanded = set(tokens)
    for token in list(tokens):
        expanded |= SYNONYMS.get(token, set())
        if token.endswith("s") and len(token) > 4:
            expanded.add(token[:-1])
    return expanded


def infer_action(query: str, card: dict[str, Any]) -> ActionType:
    """Infer the intended action, never silently downgrading a destructive intent.

    If the user asks to delete and the agent is not allowed to delete, the intent
    is preserved so policy/approval can deny it. Harmless mismatches fall back to
    a read-class action the agent is allowed to perform.
    """

    query_tokens = set(tokenize(query))  # no synonym expansion: intent must be literal
    allowed = {str(item) for item in card.get("allowed_actions") or []}

    intended: ActionType | None = None
    for action, keywords in ACTION_KEYWORDS:
        if query_tokens & keywords:
            intended = action
            break

    if intended is not None:
        if not allowed or str(intended) in allowed:
            return intended
        if intended.is_high_impact:
            return intended

    if allowed:
        for candidate in (ActionType.QUERY, ActionType.READ, ActionType.ANALYZE):
            if str(candidate) in allowed:
                return candidate
        try:
            return ActionType(next(iter(allowed)))
        except (StopIteration, ValueError):
            return ActionType.QUERY
    return ActionType.QUERY


def score_card(card: dict[str, Any], query_tokens: set[str]) -> float:
    score = 0.0
    capabilities = [str(item) for item in card.get("capabilities") or []]
    tags = [str(item) for item in card.get("tags") or []]
    examples = [str(item) for item in card.get("examples") or []]
    name = str(card.get("name") or "")
    description = str(card.get("description") or "")

    for capability in capabilities:
        capability_tokens = expand(set(tokenize(capability)))
        if not capability_tokens:
            continue
        overlap = capability_tokens & query_tokens
        if overlap:
            score += 3.0 * len(overlap)
            if capability_tokens.issubset(query_tokens):
                score += 2.0

    for tag in tags:
        tag_tokens = expand(set(tokenize(tag)))
        if tag_tokens & query_tokens:
            score += 2.0

    for example in examples[:5]:
        example_tokens = expand(set(tokenize(example)))
        overlap = example_tokens & query_tokens
        if overlap:
            score += 1.5 * len(overlap)

    name_tokens = expand(set(tokenize(name)))
    score += 1.5 * len(name_tokens & query_tokens)

    description_tokens = expand(set(tokenize(description)))
    score += 0.4 * len(description_tokens & query_tokens)
    return round(score, 3)


def heuristic_plan(
    *,
    query: str,
    catalog: list[dict[str, Any]],
    settings: Settings,
    confidence_floor: float = 0.0,
) -> Plan:
    query_tokens = expand(set(tokenize(query)))
    if not query_tokens or not catalog:
        return Plan(
            intent=query[:300],
            confidence=0.0,
            no_match_reason="The request does not contain enough information to select an agent.",
            source="heuristic",
        )

    ranked: list[tuple[float, dict[str, Any]]] = []
    for card in catalog:
        score = score_card(card, query_tokens)
        if score > 0:
            ranked.append((score, card))
    ranked.sort(key=lambda item: item[0], reverse=True)

    if not ranked or ranked[0][0] < 2.0:
        return Plan(
            intent=query[:300],
            confidence=0.0,
            no_match_reason="No registered agent matched the request.",
            source="heuristic",
        )

    # A weak incidental token hit (e.g. the word "check" in a generic
    # description) must not drag unrelated agents into the plan.
    top_score = ranked[0][0]
    inclusion_floor = max(2.0, top_score * 0.35)
    ranked = [item for item in ranked if item[0] >= inclusion_floor]

    selected: list[PlannedAgent] = []
    for score, card in ranked[: settings.planner_max_agents]:
        confidence = min(0.95, score / 10.0)
        if confidence < confidence_floor:
            continue
        action = infer_action(query, card)
        selected.append(
            PlannedAgent(
                agent_id=card["agent_id"],
                reason=f"Matched capabilities {', '.join(card.get('capabilities', [])[:2])} (score {score:.1f}).",
                instructions="Answer the user's request using your domain capabilities. Be explicit about assumptions.",
                capability=(card.get("capabilities") or [None])[0],
                action=action,
                priority=len(selected) + 1,
            )
        )

    if not selected:
        return Plan(
            intent=query[:300],
            confidence=0.0,
            no_match_reason="No registered agent matched the request with sufficient confidence.",
            source="heuristic",
        )

    top_confidence = max(min(0.95, score / 10.0) for score, _ in ranked[:1])
    return Plan(
        intent=query[:300],
        use_case=str((ranked[0][1].get("tags") or ["general"])[0]),
        summary=f"Selected {len(selected)} agent(s) based on capability match.",
        selected_agents=selected,
        confidence=top_confidence,
        requires_human_approval=any(agent.action.is_high_impact for agent in selected),
        approval_reason=(
            "High-impact action inferred from the request."
            if any(agent.action.is_high_impact for agent in selected)
            else None
        ),
        source="heuristic",
    )


def validate_plan(payload: dict[str, Any], *, catalog: list[dict[str, Any]], settings: Settings) -> Plan:
    """Turn an untrusted model payload into a safe plan against a closed catalog."""

    by_id = {card["agent_id"]: card for card in catalog}
    rejected: list[dict[str, Any]] = []
    selected: list[PlannedAgent] = []
    seen: set[str] = set()

    for item in payload.get("selected_agents") or []:
        if not isinstance(item, dict):
            continue
        agent_id = str(item.get("agent_id") or "").strip()
        if not agent_id or agent_id in seen:
            continue
        if agent_id not in by_id:
            rejected.append({"agent_id": agent_id, "reason": "not_in_registry"})
            continue
        seen.add(agent_id)
        try:
            action = ActionType(str(item.get("action") or "query").lower())
        except ValueError:
            action = ActionType.QUERY
        card = by_id[agent_id]
        allowed = {str(value) for value in card.get("allowed_actions") or []}
        if allowed and str(action) not in allowed:
            # The model asked for something this agent may not do. Never run it:
            # policy would deny it anyway, and silently downgrading the action
            # would execute a different intent than the one the user asked for.
            rejected.append({"agent_id": agent_id, "reason": f"action '{action}' not permitted"})
            continue
        try:
            priority = int(item.get("priority") or len(selected) + 1)
        except (TypeError, ValueError):
            priority = len(selected) + 1
        selected.append(
            PlannedAgent(
                agent_id=agent_id,
                reason=str(item.get("reason") or "")[:500],
                instructions=str(item.get("instructions") or "")[:2000],
                capability=str(item.get("capability") or "") or None,
                action=action,
                priority=priority,
            )
        )

    selected = selected[: settings.planner_max_agents]
    try:
        confidence = max(0.0, min(1.0, float(payload.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        confidence = 0.0

    no_match_reason = payload.get("no_match_reason")
    if not selected and not no_match_reason:
        no_match_reason = "The planner did not find a registered agent able to answer this request."

    return Plan(
        intent=str(payload.get("intent") or "")[:500],
        use_case=str(payload.get("use_case") or "general")[:120],
        summary=str(payload.get("summary") or "")[:500],
        selected_agents=sorted(selected, key=lambda agent: agent.priority),
        requires_human_approval=bool(payload.get("requires_human_approval")),
        approval_reason=payload.get("approval_reason"),
        confidence=confidence,
        no_match_reason=str(no_match_reason)[:500] if no_match_reason else None,
        source="llm",
        rejected_agents=rejected,
    )


class Planner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def plan(
        self,
        *,
        query: str,
        catalog: list[dict[str, Any]],
        roles: list[str],
        environment: str,
        use_case: str,
        gateway: ModelGateway,
        conversation: list[dict[str, str]] | None = None,
    ) -> Plan:
        heuristic = heuristic_plan(query=query, catalog=catalog, settings=self.settings)

        if gateway.is_mock:
            return heuristic

        if not catalog:
            return Plan(
                intent=query[:300],
                confidence=0.0,
                no_match_reason="No agents are registered for this environment.",
                source="registry",
            )

        try:
            payload, _ = await gateway.complete_json(
                system=PLANNER_SYSTEM,
                user=build_planner_user_prompt(
                    query=query,
                    catalog=catalog,
                    roles=roles,
                    environment=environment,
                    use_case=use_case,
                    conversation=conversation,
                ),
            )
        except Exception as exc:
            logger.warning("LLM planner failed, falling back to heuristic: %s", exc)
            heuristic.source = "heuristic-fallback"
            return heuristic

        if not payload:
            logger.warning("LLM planner returned unparsable JSON; falling back to heuristic.")
            heuristic.source = "heuristic-fallback"
            return heuristic

        plan = validate_plan(payload, catalog=catalog, settings=self.settings)

        # Hybrid safety net: if the model both selected nothing and is not
        # confident, trust a strong deterministic match over "no match".
        if not plan.selected_agents:
            if heuristic.selected_agents and heuristic.confidence >= self.settings.planner_min_confidence:
                heuristic.source = "hybrid-heuristic"
                return heuristic
            return plan

        if plan.confidence < self.settings.planner_min_confidence and heuristic.selected_agents:
            # Low-confidence model plan: union with the heuristic picks so an
            # obvious capability match is never dropped.
            merged = {agent.agent_id: agent for agent in plan.selected_agents}
            for candidate in heuristic.selected_agents:
                merged.setdefault(candidate.agent_id, candidate)
            plan.selected_agents = sorted(merged.values(), key=lambda agent: agent.priority)[
                : self.settings.planner_max_agents
            ]
            plan.source = "hybrid"
            plan.confidence = max(plan.confidence, heuristic.confidence)

        # Conversely, a high-confidence heuristic match that the model missed is
        # added when it is very strong.
        if heuristic.selected_agents and heuristic.confidence >= 0.7:
            existing = {agent.agent_id for agent in plan.selected_agents}
            extras = [
                agent
                for agent in heuristic.selected_agents
                if agent.agent_id not in existing and agent.priority == 1
            ]
            if extras and len(plan.selected_agents) < self.settings.planner_max_agents:
                plan.selected_agents.extend(extras[: self.settings.planner_max_agents - len(plan.selected_agents)])
                plan.source = "hybrid"

        return plan
