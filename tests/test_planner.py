"""Planner selection logic: heuristic, LLM validation and hybrid fallbacks."""

from __future__ import annotations

from alstom_ao.config import Settings
from alstom_ao.domain.enums import ActionType
from alstom_ao.llm.gateway import LLMBudget, ScriptedGateway
from alstom_ao.orchestrator.planner import Planner, heuristic_plan, validate_plan

CATALOG = [
    {
        "agent_id": "finance-invoice-auditor",
        "name": "Finance Invoice Auditor",
        "description": "Audits vendor invoices and detects duplicate charges.",
        "capabilities": ["invoice audit", "duplicate invoice detection"],
        "tags": ["finance", "invoice"],
        "examples": ["Check invoice INV-1 for duplicate charges"],
        "risk_level": "high",
        "allowed_actions": ["read", "query", "analyze", "write"],
        "requires_approval_for": ["write"],
        "version": "2.3.0",
        "owner": "Finance",
    },
    {
        "agent_id": "hr-policy-advisor",
        "name": "HR Policy Advisor",
        "description": "Answers leave and policy questions.",
        "capabilities": ["leave and absence rules", "hr policy lookup"],
        "tags": ["hr", "leave"],
        "examples": ["How many days of annual leave do I get?"],
        "risk_level": "low",
        "allowed_actions": ["read", "query"],
        "requires_approval_for": [],
        "version": "1.0.0",
        "owner": "People",
    },
]


def settings(**overrides) -> Settings:
    return Settings(
        environment="test",
        database_url="sqlite+aiosqlite:///:memory:",
        opa_url=None,
        llm_mode="mock",
        **overrides,
    )


def test_heuristic_picks_the_capability_match():
    plan = heuristic_plan(
        query="Check invoice INV-2024-0912 for duplicate charges",
        catalog=CATALOG,
        settings=settings(),
    )
    assert [agent.agent_id for agent in plan.selected_agents] == ["finance-invoice-auditor"]
    assert plan.selected_agents[0].action == ActionType.ANALYZE
    assert plan.confidence > 0.5


def test_heuristic_returns_no_match_for_unrelated_question():
    plan = heuristic_plan(
        query="What is the airspeed velocity of an unladen swallow?",
        catalog=CATALOG,
        settings=settings(),
    )
    assert plan.selected_agents == []
    assert plan.no_match_reason


def test_heuristic_infers_destructive_action():
    plan = heuristic_plan(
        query="Delete the duplicate invoice charge for INV-2024-0912",
        catalog=CATALOG,
        settings=settings(),
    )
    assert plan.selected_agents[0].action == ActionType.DELETE
    assert plan.requires_human_approval is True


def test_heuristic_respects_allowed_actions():
    plan = heuristic_plan(
        query="Delete my annual leave request",
        catalog=[CATALOG[1]],
        settings=settings(),
    )
    # HR agent cannot delete; the destructive intent is preserved so policy can
    # deny it, and it never executes as a harmless read.
    assert plan.selected_agents
    assert plan.selected_agents[0].action == ActionType.DELETE
    assert plan.requires_human_approval is True


def test_validate_plan_drops_unknown_agents_and_disallowed_actions():
    plan = validate_plan(
        {
            "intent": "test",
            "confidence": 0.9,
            "selected_agents": [
                {"agent_id": "ghost-agent", "action": "query"},
                {"agent_id": "finance-invoice-auditor", "action": "delete"},
            ],
        },
        catalog=CATALOG,
        settings=settings(),
    )
    assert plan.selected_agents == []
    reasons = {item["agent_id"]: item["reason"] for item in plan.rejected_agents}
    assert reasons["ghost-agent"] == "not_in_registry"
    assert "not permitted" in reasons["finance-invoice-auditor"]


async def test_llm_plan_is_validated_and_used():
    gateway = ScriptedGateway(
        settings(),
        LLMBudget(max_calls=5, max_tokens=1000, max_cost_usd=1),
        responses=[
            """{"intent":"invoice check","use_case":"finance","confidence":0.82,
            "selected_agents":[{"agent_id":"finance-invoice-auditor","reason":"matches","action":"analyze"}],
            "requires_human_approval":false}"""
        ],
    )
    planner = Planner(settings())
    plan = await planner.plan(
        query="Check invoice INV-1 for duplicate charges",
        catalog=CATALOG,
        roles=["user"],
        environment="test",
        use_case="general",
        gateway=gateway,
    )
    assert plan.source in {"llm", "hybrid"}
    assert plan.selected_agents[0].agent_id == "finance-invoice-auditor"


async def test_invalid_llm_json_falls_back_to_heuristic():
    gateway = ScriptedGateway(
        settings(),
        LLMBudget(max_calls=5, max_tokens=1000, max_cost_usd=1),
        responses=["I cannot answer that."],
    )
    planner = Planner(settings())
    plan = await planner.plan(
        query="Check invoice INV-1 for duplicate charges",
        catalog=CATALOG,
        roles=["user"],
        environment="test",
        use_case="general",
        gateway=gateway,
    )
    assert plan.source == "heuristic-fallback"
    assert plan.selected_agents[0].agent_id == "finance-invoice-auditor"


async def test_hallucinated_agent_is_rejected_and_heuristic_kept():
    gateway = ScriptedGateway(
        settings(),
        LLMBudget(max_calls=5, max_tokens=1000, max_cost_usd=1),
        responses=[
            """{"intent":"x","confidence":0.2,"selected_agents":[{"agent_id":"invented-agent","action":"query"}]}"""
        ],
    )
    planner = Planner(settings())
    plan = await planner.plan(
        query="Check invoice INV-1 for duplicate charges",
        catalog=CATALOG,
        roles=["user"],
        environment="test",
        use_case="general",
        gateway=gateway,
    )
    assert plan.selected_agents[0].agent_id == "finance-invoice-auditor"


async def test_unknown_agents_only_returns_no_match():
    gateway = ScriptedGateway(
        settings(),
        LLMBudget(max_calls=5, max_tokens=1000, max_cost_usd=1),
        responses=["""{"intent":"x","confidence":0.1,"selected_agents":[{"agent_id":"ghost","action":"query"}]}"""],
    )
    planner = Planner(settings())
    plan = await planner.plan(
        query="Tell me something unrelated",
        catalog=CATALOG,
        roles=["user"],
        environment="test",
        use_case="general",
        gateway=gateway,
    )
    assert plan.selected_agents == []
    assert plan.rejected_agents[0]["agent_id"] == "ghost"
