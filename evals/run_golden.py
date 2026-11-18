"""Golden-set evaluation for routing, status and grounding.

Run against the deterministic heuristic (no keys, fast, CI-friendly):

    python -m evals.run_golden

Run against the live prod stack (real LLM planner, real costs):

    python -m evals.run_golden --base-url http://localhost:8080

Exit code is non-zero on any failure, so it works as a gate in CI.

Failure taxonomy (mirrors nod's eval style): routing_miss, routing_extra,
false_no_match, missed_no_match, wrong_status, error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "evals" / "golden_routing.json"

USER_HEADERS = {"X-User-Id": "eval-runner", "X-User-Roles": "user,finance"}


@dataclass
class CaseResult:
    case_id: str
    failures: list[str] = field(default_factory=list)
    status: str = ""
    agents: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def ok(self) -> bool:
        return not self.failures


def evaluate(case: dict, *, status: str, agents: list[str]) -> CaseResult:
    """Compare one observed run with its expectation."""

    result = CaseResult(case_id=case["id"], status=status, agents=agents, note=case.get("note", ""))
    expected = list(case.get("expect_agents") or [])
    must_not = list(case.get("must_not") or [])
    expect_status = case.get("expect_status")

    missing = [agent for agent in expected if agent not in agents]
    extra = [agent for agent in must_not if agent in agents]

    if missing and not expected:
        pass
    if missing:
        # An agent that should have been chosen is missing: either a plain miss or a
        # false no_match, which is a distinct and worse failure.
        result.failures.append("false_no_match" if status == "no_match" else "routing_miss")
    # Selecting anything when nothing should answer is the inverse error.
    if not expected and agents:
        result.failures.append("missed_no_match")
    if extra:
        result.failures.append("routing_extra")
    if expect_status and status != expect_status:
        result.failures.append("wrong_status")
    return result


def applies(case: dict, mode: str) -> bool:
    case_mode = case.get("mode", "both")
    return case_mode in {"both", mode}


# ---------------------------------------------------------------------------
# offline: heuristic planner only
# ---------------------------------------------------------------------------


def _load_cards(cards_path: Path) -> list[dict]:
    """Read card fixtures off the event loop (the runner is async)."""

    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(cards_path.glob("*.json"))]


async def run_mock(cards_path: Path) -> list[CaseResult]:
    from alstom_ao.config import Settings
    from alstom_ao.db.session import Database
    from alstom_ao.domain.models import AgentCard
    from alstom_ao.orchestrator.planner import heuristic_plan

    settings = Settings(environment="eval", database_url="sqlite+aiosqlite:///:memory:")
    cards = await asyncio.to_thread(_load_cards, cards_path)
    for card in cards:
        card["environment"] = "eval"
    catalog = [
        {
            "agent_id": card["agent_id"],
            "name": card["name"],
            "description": card["description"],
            "capabilities": card["capabilities"],
            "tags": card.get("tags", []),
            "examples": card.get("examples", []),
            "risk_level": card.get("risk_level", "low"),
            "allowed_actions": card["permissions"].get("allowed_actions", []),
            "requires_approval_for": card["permissions"].get("requires_approval_for", []),
            "version": card.get("version", "1.0.0"),
            "owner": card.get("owner", ""),
        }
        for card in cards
    ]
    # Fail loudly if the fixtures themselves are invalid.
    for card in cards:
        AgentCard(**card)

    database = Database(settings)
    await database.dispose()

    results: list[CaseResult] = []
    for case in json.loads(GOLDEN.read_text(encoding="utf-8"))["cases"]:
        if not applies(case, "mock"):
            continue
        plan = heuristic_plan(query=case["query"], catalog=catalog, settings=settings)
        agents = [agent.agent_id for agent in plan.selected_agents]
        status = "no_match" if not agents else "planned"
        expected_status = case.get("expect_status")
        # In mock mode there is no approval/policy phase, so only compare status
        # when the case genuinely expects a terminal routing outcome.
        if expected_status in {"completed", "awaiting_approval"}:
            expected_status = "planned"
        results.append(evaluate({**case, "expect_status": expected_status}, status=status, agents=agents))
    return results


# ---------------------------------------------------------------------------
# live: real HTTP against the stack
# ---------------------------------------------------------------------------


async def run_live(base_url: str) -> list[CaseResult]:
    import httpx

    results: list[CaseResult] = []
    async with httpx.AsyncClient(base_url=base_url, timeout=180.0) as client:
        for case in json.loads(GOLDEN.read_text(encoding="utf-8"))["cases"]:
            if not applies(case, "real"):
                continue
            try:
                response = await client.post(
                    "/v1/orchestrate", json={"query": case["query"]}, headers=USER_HEADERS
                )
                response.raise_for_status()
                run = response.json()["run"]
            except Exception as exc:  # a broken case must not hide the rest
                results.append(CaseResult(case_id=case["id"], failures=["error"], note=str(exc)[:120]))
                continue
            agents = [agent["agent_id"] for agent in (run.get("plan") or {}).get("selected_agents", [])]
            results.append(evaluate(case, status=run["status"], agents=agents))
    return results


def summarise(results: list[CaseResult]) -> int:
    passed = [result for result in results if result.ok]
    failed = [result for result in results if not result.ok]

    for result in results:
        mark = "PASS" if result.ok else "FAIL"
        detail = "" if result.ok else "  <- " + ", ".join(result.failures)
        agents = ",".join(result.agents) or "-"
        print(f"{mark}  {result.case_id:28s} status={result.status:18s} agents={agents}{detail}")

    counts: dict[str, int] = {}
    for result in failed:
        for failure in result.failures:
            counts[failure] = counts.get(failure, 0) + 1

    print()
    print(f"{len(passed)}/{len(results)} passed")
    if counts:
        print("failures: " + ", ".join(f"{name}={count}" for name, count in sorted(counts.items())))
    return 0 if not failed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default=None, help="live stack URL; omit for the offline heuristic")
    parser.add_argument(
        "--cards",
        default=str(ROOT / "examples" / "agent_cards"),
        help="directory of agent cards used as the offline catalog",
    )
    args = parser.parse_args(argv)

    if args.base_url:
        os.environ.setdefault("AO_ENVIRONMENT", "eval")
        results = asyncio.run(run_live(args.base_url.rstrip("/")))
    else:
        results = asyncio.run(run_mock(Path(args.cards)))
    return summarise(results)


if __name__ == "__main__":
    sys.exit(main())
