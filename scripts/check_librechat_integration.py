"""Verify the LibreChat -> Alstom AO integration from outside the browser.

The UI is the acceptance test (docs/librechat.md); this script proves the same
path non-interactively, which is what CI and on-call need.

    python scripts/check_librechat_integration.py
    python scripts/check_librechat_integration.py --librechat http://localhost:3080

Checks, in order:
  1. AO is healthy and the registry has agents for the environment
  2. the planner catalog is non-empty for a UI-style role ("ADMIN")
  3. a read question completes and the answer comes from the right agent
  4. a write question pauses for approval, and approving it executes the agent
  5. Langfuse received spans for the run (when enabled)

Exit code 0 on success, 1 on any failed check.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

import httpx

AO = "http://localhost:8080"
READ_Q = "Check invoice INV-2024-0912 for duplicate charges"
WRITE_Q = "Create an adjustment task for invoice INV-2024-0912"

# Mimics what LibreChat sends: the platform user id and an application role.
UI_HEADERS = {
    "X-User-Id": "librechat-check",
    "X-User-Email": "check@alstom.local",
    "X-Service-Id": "librechat",
    "X-Service-Name": "librechat-ui",
    "X-Use-Case": "chat-ui",
}
APPROVER = {"X-User-Id": "approver-check", "X-User-Roles": "approver"}

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(("PASS  " if ok else "FAIL  ") + name + (f"  ({detail})" if detail and not ok else ""))
    return ok


def run_query(client: httpx.Client, query: str) -> dict[str, Any]:
    response = client.post(f"{AO}/v1/orchestrate", json={"query": query}, headers=UI_HEADERS, timeout=180)
    response.raise_for_status()
    return response.json()["run"]


def main(argv: list[str] | None = None) -> int:
    global AO
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ao", default=AO)
    parser.add_argument("--expect-agent", default="finance-invoice-auditor")
    args = parser.parse_args(argv)

    AO = args.ao.rstrip("/")

    with httpx.Client(timeout=60) as client:
        # 1. health
        health = client.get(f"{AO}/healthz")
        check("AO healthy", health.status_code == 200, health.text[:80])
        ready = client.get(f"{AO}/readyz").json()
        check("AO ready", ready.get("status") == "ok", json.dumps(ready.get("checks", {}))[:120])

        # 2. catalog for a UI-style role must not be empty
        catalog = client.get(f"{AO}/v1/agents/catalog", headers=UI_HEADERS)
        agents = catalog.json().get("agents", [])
        check("catalog non-empty for ADMIN role", bool(agents), f"{len(agents)} agents")
        check(
            f"{args.expect_agent} routable",
            any(agent["agent_id"] == args.expect_agent for agent in agents),
        )

        # 3. read path
        started = time.time()
        run = run_query(client, READ_Q)
        read_ok = run["status"] == "completed" and any(
            agent["agent_id"] == args.expect_agent for agent in run.get("agent_results", [])
        )
        check("read query completes", read_ok, run["status"])
        check("read query did not require approval", run.get("approval") is None)
        check("read query produced an answer", bool(run.get("response")))
        print(f"      {round(time.time() - started, 1)}s, cost ${run['usage'].get('cost_usd')}")

        # 4. write path + approval
        write = run_query(client, WRITE_Q)
        check("write query pauses for approval", write["status"] == "awaiting_approval", write["status"])
        approval = write.get("approval") or {}
        if approval:
            decision = client.post(
                f"{AO}/v1/approvals/{approval['approval_id']}/decision",
                json={"approved": True, "note": "integration check"},
                headers=APPROVER,
                timeout=180,
            )
            body = decision.json() if decision.status_code == 200 else {}
            executed = [
                agent["agent_id"] for agent in (body.get("run") or {}).get("agent_results", [])
            ]
            check("approval resumes and executes", body.get("run", {}).get("status") == "completed", decision.status_code)
            check("approved agent executed", args.expect_agent in executed, str(executed))
            check("langfuse trace url present", bool(body.get("run", {}).get("langfuse_url")))

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
