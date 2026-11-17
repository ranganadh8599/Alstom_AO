"""Numeric grounding: the synthesizer must not compute numbers no agent reported.

Regression under test: given "25 days per calendar year" and "plus 5 days after
three years", the synthesizer answered "a total of 80 days". Prompt rules alone
cannot prevent that, so numbers in the answer are verified against agent output.
"""

from __future__ import annotations

import httpx
import pytest

from alstom_ao.config import Settings
from alstom_ao.llm.grounding import check_grounding, extract_numbers
from alstom_ao.main import create_app, initialize_app, shutdown_app
from alstom_ao.orchestrator import service as run_service_module
from tests.conftest import FakeA2ANetwork, agent_payload

AGENT_ANSWER = (
    "Under HR policy 4.2, employees accrue 25 days of annual leave per calendar year, "
    "plus 5 days after three years of service. Requests over 10 consecutive days need "
    "manager approval. Source: HR Policy 2024.4, section 4.2."
)

HR = dict(
    agent_id="hr-policy-advisor",
    endpoint="http://test/agents/hr",
    capabilities=["leave and absence rules", "hr policy lookup"],
    tags=["hr", "leave"],
    examples=["How many days of annual leave do I get after three years?"],
    allowed_roles=["user"],
    allowed_actions=["read", "query", "analyze"],
    risk_level="low",
)
QUERY = "How many days of annual leave do I get after three years?"


# ---------------------------------------------------------------------------
# extractor
# ---------------------------------------------------------------------------


def test_extracts_quantities_and_ignores_identifiers():
    numbers = extract_numbers(
        "25 days per year, 4% above contract, 1,200 EUR, INV-2024-0912, v2.3.0, PO-7781, 2024-Q3"
    )
    assert {"25", "4", "1200"} <= numbers
    # Identifiers must not be treated as quantities.
    assert "0912" not in numbers
    assert "7781" not in numbers
    assert "2" not in numbers


def test_normalizes_thousands_and_decimals():
    assert extract_numbers("paid 1,500.50 and 3") >= {"1500.5", "3"}


def test_fabricated_total_is_flagged():
    """The exact production bug: 80 was computed, never reported."""

    report = check_grounding(
        "You will receive a total of 80 days of annual leave after three years.",
        [AGENT_ANSWER],
        query=QUERY,
    )
    assert report.ungrounded == ["80"]
    assert "80" in report.describe()


def test_reiterating_agent_numbers_passes():
    report = check_grounding(
        "You accrue 25 days of annual leave per calendar year, plus 5 days after three years. "
        "Requests over 10 consecutive days need manager approval (HR Policy 2024.4).",
        [AGENT_ANSWER],
        query=QUERY,
    )
    assert report.ok, report.ungrounded


def test_numbers_from_the_question_are_allowed():
    """Echoing "three" is prose; echoing a user-supplied figure is legitimate."""

    report = check_grounding("You asked about 7 days; policy grants 25 per year.", [AGENT_ANSWER], query="I want 7 days")
    assert report.ok


def test_bare_years_are_not_flagged():
    report = check_grounding("The policy was published in 2024 and revised in 2019.", [AGENT_ANSWER])
    assert report.ok


# ---------------------------------------------------------------------------
# modes
# ---------------------------------------------------------------------------


def _gateway(monkeypatch, answer: str):
    from alstom_ao.llm.gateway import ScriptedGateway

    def fake_build(settings, budget, *, responses=None):
        # First call: planner (unparsable -> heuristic). Then the synthesizer.
        return ScriptedGateway(settings, budget, responses=["{}", answer] * 100)

    monkeypatch.setattr(run_service_module, "build_gateway", fake_build)


async def _run(app, headers, a2a: FakeA2ANetwork) -> dict:
    a2a.answers["/agents/hr"] = AGENT_ANSWER
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        admin = {"X-User-Id": "admin", "X-User-Roles": "platform-admin"}
        await client.post("/v1/agents", json=agent_payload(**HR), headers=admin)
        response = await client.post("/v1/orchestrate", json={"query": QUERY}, headers=headers)
        assert response.status_code == 200, response.text
        return response.json()["run"]


async def _trace(app, run_id: str, headers) -> list[dict]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get(f"/v1/runs/{run_id}/trace", headers=headers)
        return r.json()["events"]


def _settings(grounding: str) -> Settings:
    return Settings(
        environment="test",
        database_url="sqlite+aiosqlite:///:memory:",
        seed_demo_agents=False,
        opa_url=None,
        llm_mode="mock",
        synthesis_grounding=grounding,
    )


@pytest.fixture
async def grounding_app_factory(a2a):
    created = []

    async def make(grounding: str):
        app = create_app(_settings(grounding), a2a_transport=a2a.transport)
        await initialize_app(app)
        created.append(app)
        return app

    yield make
    for app in created:
        await shutdown_app(app)


async def test_warn_mode_keeps_answer_but_adds_caveat(grounding_app_factory, user_headers, monkeypatch, a2a):
    app = await grounding_app_factory("warn")
    _gateway(monkeypatch, "You will receive a total of 80 days of annual leave.")
    run = await _run(app, user_headers, a2a)

    assert run["status"] == "completed"
    assert "80 days" in run["response"]  # answer preserved
    assert "not present in any agent result" in run["response"]
    assert "80" in run["response"].split("unverified")[0]

    events = await _trace(app, run["run_id"], user_headers)
    ungrounded = [e for e in events if e["action"] == "synthesis.ungrounded"]
    assert ungrounded and ungrounded[0]["output"]["ungrounded"] == ["80"]
    assert ungrounded[0]["output"]["action"] == "warned"


async def test_strict_mode_withholds_the_fabricated_total(grounding_app_factory, user_headers, monkeypatch, a2a):
    app = await grounding_app_factory("strict")
    _gateway(monkeypatch, "You will receive a total of 80 days of annual leave.")
    run = await _run(app, user_headers, a2a)

    assert run["status"] == "completed"
    # The fabricated total must not reach the user as fact.
    assert "total of 80" not in run["response"]
    assert "withheld" in run["response"]
    # Verbatim agent text is shown instead, so the user still gets the real numbers.
    assert "25 days" in run["response"]
    assert "5 days" in run["response"]

    events = await _trace(app, run["run_id"], user_headers)
    ungrounded = [e for e in events if e["action"] == "synthesis.ungrounded"]
    assert ungrounded and ungrounded[0]["output"]["action"] == "strict_withheld"


async def test_grounded_answer_passes_untouched(grounding_app_factory, user_headers, monkeypatch, a2a):
    app = await grounding_app_factory("strict")
    _gateway(
        monkeypatch,
        "You accrue 25 days of annual leave per calendar year, plus 5 days after three years.",
    )
    run = await _run(app, user_headers, a2a)

    assert "withheld" not in run["response"]
    assert "not present in any agent result" not in run["response"]
    assert "25 days" in run["response"]

    events = await _trace(app, run["run_id"], user_headers)
    assert not [e for e in events if e["action"] == "synthesis.ungrounded"]


async def test_off_mode_disables_the_check(grounding_app_factory, user_headers, monkeypatch, a2a):
    app = await grounding_app_factory("off")
    _gateway(monkeypatch, "You will receive a total of 80 days.")
    run = await _run(app, user_headers, a2a)

    assert "80 days" in run["response"]
    assert "not present in any agent result" not in run["response"]
    events = await _trace(app, run["run_id"], user_headers)
    assert not [e for e in events if e["action"] == "synthesis.ungrounded"]


def test_invalid_mode_is_rejected():
    with pytest.raises(ValueError):
        Settings(environment="test", synthesis_grounding="maybe")

# ---------------------------------------------------------------------------
# limitation 1: literal matching is too brittle
# ---------------------------------------------------------------------------


def test_decimal_restatement_is_not_flagged():
    """'"4%" vs "4.0%" is the same quantity."""

    report = check_grounding("The line item is 4.0% above contract.", ["One line item is 4% above contract"])
    assert report.ok, report.ungrounded


def test_integer_vs_decimal_trailing_zero_is_not_flagged():
    report = check_grounding("You get 25.0 days per year.", ["employees accrue 25 days per year"])
    assert report.ok, report.ungrounded


def test_thousands_separator_is_not_flagged():
    report = check_grounding("The budget is $1,200 per seat.", ["budget of 1200 USD per seat"])
    assert report.ok, report.ungrounded


def test_spelled_number_restatement_is_not_flagged():
    """Model writing "3 years" where the agent wrote "three years"."""

    report = check_grounding("After 3 years, you get 5 extra days.", ["plus 5 days after three years of service"])
    assert report.ok, report.ungrounded


def test_tolerance_does_not_hide_arithmetic():
    """80 vs 75 must still be caught: tolerance is representation only."""

    report = check_grounding("A total of 80 days.", ["25 days per year", "plus 5 after three years"])
    assert report.ungrounded == ["80"]


def test_redaction_removes_only_the_offending_sentence():
    from alstom_ao.llm.grounding import redact_sentences

    answer = (
        "Under HR policy 4.2 you accrue 25 days per year. "
        "Therefore you get a total of 80 days. "
        "Requests over 10 consecutive days need approval."
    )
    kept, removed, count = redact_sentences(answer, known=[25.0, 10.0, 4.0, 2.0, 5.0])
    assert "80" not in kept
    assert "25 days" in kept and "10 consecutive" in kept
    assert count == 1
    assert removed == ["80"]


def test_strict_redacts_instead_of_discarding_everything():
    from alstom_ao.llm.grounding import apply_grounding

    answer = (
        "Under HR policy 4.2 you accrue 25 days per year. "
        "Therefore you get a total of 80 days. "
        "Requests over 10 consecutive days need approval."
    )
    text, report, action = apply_grounding(
        answer=answer,
        results=["employees accrue 25 days per year, plus 5 after three years, request over 10 days needs approval"],
        query="How many days?",
        mode="strict",
        fallback="verbatim agent text",
    )
    assert action == "strict_redacted"
    assert "total of 80" not in text  # the claim is gone; the note may name it`r`n    assert "25 days" in text
    assert "Removed 1 sentence" in text


def test_failed_repair_falls_back_to_original_verdict():
    """A degenerate repair must not launder a fabricated figure."""

    from alstom_ao.llm.grounding import apply_grounding

    text, report, action = apply_grounding(
        answer="A total of 80 days.",
        results=["25 days per year"],
        query="How many?",
        mode="warn",
        fallback="25 days per year",
        repaired_answer="{}",
    )
    assert action == "warned"
    assert "80" in text and "unverified" in text


def test_successful_repair_is_accepted():
    from alstom_ao.llm.grounding import apply_grounding

    text, report, action = apply_grounding(
        answer="A total of 80 days.",
        results=["employees accrue 25 days per year, plus 5 after three years"],
        query="How many?",
        mode="strict",
        fallback="25 days per year",
        repaired_answer="You accrue 25 days per year, plus 5 after three years of service.",
    )
    assert action == "repaired"
    assert "80" not in text


# ---------------------------------------------------------------------------
# limitation 3: an agent can be wrong (make it visible, do not pretend to fix)
# ---------------------------------------------------------------------------


def test_cross_agent_disagreement_is_reported():
    from alstom_ao.llm.grounding import check_agent_consistency

    results = [
        {"agent_id": "a1", "structured": {"balance": 1200.0}},
        {"agent_id": "a2", "structured": {"balance": 1500.0}},
    ]
    check = check_agent_consistency(results, {"a1": "", "a2": ""})
    assert not check.ok
    assert check.disagreements[0]["metric"] == "balance"
    assert set(check.disagreements[0]["agents"]) == {"a1", "a2"}


def test_agreeing_agents_pass():
    from alstom_ao.llm.grounding import check_agent_consistency

    results = [
        {"agent_id": "a1", "structured": {"balance": 1200.0}},
        {"agent_id": "a2", "structured": {"balance": 1200.00}},
    ]
    assert check_agent_consistency(results, {"a1": "", "a2": ""}).ok


def test_agent_prose_contradicting_its_own_structured_output_is_flagged():
    from alstom_ao.llm.grounding import check_agent_consistency

    results = [{"agent_id": "a1", "structured": {"balance": 1200.0}}]
    check = check_agent_consistency(results, {"a1": "The vendor balance is 9500 units."})
    assert "a1:9500" in check.text_contradicts_structured


def test_prose_only_agents_are_not_flagged():
    """No structured numbers means nothing to compare against: stay quiet."""

    from alstom_ao.llm.grounding import check_agent_consistency

    results = [{"agent_id": "a1", "structured": {}}]
    assert check_agent_consistency(results, {"a1": "You get 25 days per year."}).ok


def test_numeric_fields_flattens_nested_payloads():
    from alstom_ao.llm.grounding import numeric_fields

    flat = numeric_fields({"invoice": {"total": 1200.0, "lines": [{"qty": 3}]}, "paid": True})
    assert flat == {"invoice.total": 1200.0, "invoice.lines.0.qty": 3.0}
