"""Start the mock A2A agents and the orchestration API for a local demo.

The script:
1. starts ``examples.mock_a2a_agents`` on :9101
2. starts the API on :8080 with SQLite + mock LLM + seeded demo cards
3. waits for both to be healthy
4. runs a read-only question, a write question (approval), approves it, and prints the trace

Requires no API key and no Postgres.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
API = "http://localhost:8080"
AGENTS = "http://localhost:9101"
USER = {"X-User-Id": "demo-user", "X-User-Roles": "user,finance", "X-Use-Case": "demo"}
APPROVER = {"X-User-Id": "demo-approver", "X-User-Roles": "approver", "X-Use-Case": "demo"}


def wait_for(url: str, timeout: float = 40.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=1.0).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.4)
    raise SystemExit(f"Timed out waiting for {url}")


def start(args: list[str], env: dict[str, str]) -> subprocess.Popen:
    return subprocess.Popen(args, cwd=ROOT, env={**os.environ, **env})


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:  # pragma: no cover - non-standard stream
        pass
    env = {
        "PYTHONPATH": str(ROOT / "src"),
        "AO_DATABASE_URL": "sqlite+aiosqlite:///./demo.db",
        "AO_DB_AUTO_CREATE": "true",
        "AO_SEED_DEMO_AGENTS": "true",
        "AO_DEMO_AGENT_BASE_URL": AGENTS,
        "AO_LLM_MODE": "mock",
        "AO_POLICY_MODE": "observe",
        "AO_OPA_URL": "",
        "AO_REQUIRE_USER_IDENTITY": "true",
        "AO_APPROVAL_ALLOW_SELF": "false",
    }
    processes = [
        start(
            [sys.executable, "-m", "uvicorn", "examples.mock_a2a_agents.app:app", "--port", "9101", "--log-level", "warning"],
            env,
        ),
        start(
            [sys.executable, "-m", "uvicorn", "alstom_ao.main:app", "--port", "8080", "--log-level", "warning"],
            env,
        ),
    ]
    try:
        wait_for(f"{AGENTS}/healthz")
        wait_for(f"{API}/healthz")

        print("\n=== 1. Read-only question (no approval) ===")
        response = httpx.post(
            f"{API}/v1/orchestrate",
            json={"query": "Check invoice INV-2024-0912 for duplicate charges"},
            headers=USER,
            timeout=60,
        )
        run = response.json()["run"]
        print(f"status: {run['status']}")
        print(f"agents: {[agent['agent_id'] for agent in run['plan']['selected_agents']]}")
        print(run["response"][:700])

        print("\n=== 2. Write action (dynamic approval) ===")
        response = httpx.post(
            f"{API}/v1/orchestrate",
            json={"query": "Create an adjustment task for invoice INV-2024-0912"},
            headers=USER,
            timeout=60,
        )
        run = response.json()["run"]
        print(f"status: {run['status']}")
        approval = run.get("approval")
        if approval:
            print(f"approval: {approval['approval_id']} risk={approval['risk_level']}")
            print(f"reasons: {approval['reasons']}")

            print("\n=== 3. Approve and execute ===")
            decision = httpx.post(
                f"{API}/v1/approvals/{approval['approval_id']}/decision",
                json={"approved": True, "note": "demo approval"},
                headers=APPROVER,
                timeout=60,
            )
            body = decision.json()
            print(f"approval: {body['approval']['status']}")
            print(f"run: {body['run']['status']}")
            print(body["run"]["response"][:700])
            run_id = body["run"]["run_id"]
        else:
            run_id = run["run_id"]

        print("\n=== 4. Audit timeline ===")
        timeline = httpx.get(f"{API}/v1/runs/{run_id}/timeline", headers=USER, timeout=30).json()
        print(json.dumps(timeline, indent=2)[:1500])

        print("\nOpen http://localhost:8080/docs for the interactive API.")
        print("Press Ctrl+C to stop.")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        return 0
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
