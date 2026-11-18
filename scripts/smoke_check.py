"""Boot the app in-process and verify the critical endpoints answer.

This is the handover gate: it proves a fresh checkout actually runs, without a
database server, an OPA server or a model provider. Run it after install:

    python scripts/smoke_check.py

Exit code 0 means the app started, created its schema, and served health,
readiness and the agent registry.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

# Isolated settings so a developer's .env cannot change the outcome.
_TMP = Path(tempfile.mkdtemp(prefix="ao-smoke-"))
os.environ["AO_ENVIRONMENT"] = "dev"
os.environ["AO_DATABASE_URL"] = f"sqlite+aiosqlite:///{(_TMP / 'smoke.db').as_posix()}"
os.environ["AO_DB_AUTO_CREATE"] = "true"
os.environ["AO_LITELLM_PROXY_URL"] = "http://localhost:4000"
os.environ["AO_OPA_URL"] = ""
os.environ["AO_LANGFUSE_ENABLED"] = "false"

ADMIN = {"X-User-Id": "smoke-admin", "X-User-Roles": "admin"}

CHECKS: list[tuple[str, str, dict[str, str], int]] = [
    ("GET", "/healthz", {}, 200),
    ("GET", "/readyz", {}, 200),
    ("GET", "/v1/agents", ADMIN, 200),
    ("GET", "/v1/agents/catalog", ADMIN, 200),
    ("GET", "/v1/policies/status", ADMIN, 200),
]


async def main() -> int:
    import httpx

    from alstom_ao.config import Settings
    from alstom_ao.main import create_app, initialize_app, shutdown_app

    app = create_app(Settings())
    await initialize_app(app)
    failures = 0
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://smoke") as client:
            for method, path, headers, expected in CHECKS:
                response = await client.request(method, path, headers=headers)
                ok = response.status_code == expected
                failures += 0 if ok else 1
                print(f"  {'ok  ' if ok else 'FAIL'} {method} {path} -> {response.status_code}")
    finally:
        await shutdown_app(app)

    print(f"\n{len(CHECKS) - failures}/{len(CHECKS)} endpoints healthy")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
