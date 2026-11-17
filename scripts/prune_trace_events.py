"""Prune old trace events and finished runs.

The ledger is append-only by design, so it grows without bound unless something
deletes history deliberately. Run this on a schedule (cron / k8s CronJob).

    python scripts/prune_trace_events.py --db postgresql+asyncpg://... --days 90 --dry-run
    python scripts/prune_trace_events.py --days 90

What is kept:
* runs that are still `awaiting_approval` (someone may still act on them)
* runs newer than the retention window

What is removed: trace events and policy decisions of expired runs, then the runs.
Approvals follow their run via the run_id reference.

Exit codes: 0 success, 1 failure.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select

from alstom_ao.config import Settings
from alstom_ao.db import tables
from alstom_ao.db.session import Database
from alstom_ao.domain.enums import RunStatus

KEEP_STATUSES = {str(RunStatus.AWAITING_APPROVAL), str(RunStatus.PENDING), str(RunStatus.EXECUTING)}


async def prune(*, database_url: str | None, days: int, dry_run: bool, batch: int) -> dict[str, int]:
    settings = Settings(database_url=database_url) if database_url else Settings()
    database = Database(settings)
    cutoff = datetime.now(UTC) - timedelta(days=days)
    counts = {"trace_events": 0, "policy_decisions": 0, "approvals": 0, "runs": 0}

    try:
        async with database.session() as session:
            stale_runs = (
                select(tables.RunRow.run_id)
                .where(
                    tables.RunRow.created_at < cutoff,
                    tables.RunRow.status.notin_(KEEP_STATUSES),
                )
                .limit(batch)
            )
            run_ids = list((await session.execute(stale_runs)).scalars().all())
            counts["candidates"] = len(run_ids)
            if dry_run or not run_ids:
                return counts

            events = await session.execute(
                delete(tables.TraceEventRow).where(tables.TraceEventRow.run_id.in_(run_ids))
            )
            counts["trace_events"] = events.rowcount or 0

            decisions = await session.execute(
                delete(tables.PolicyDecisionRow).where(tables.PolicyDecisionRow.run_id.in_(run_ids))
            )
            counts["policy_decisions"] = decisions.rowcount or 0

            approvals = await session.execute(
                delete(tables.ApprovalRow).where(tables.ApprovalRow.run_id.in_(run_ids))
            )
            counts["approvals"] = approvals.rowcount or 0

            runs = await session.execute(delete(tables.RunRow).where(tables.RunRow.run_id.in_(run_ids)))
            counts["runs"] = runs.rowcount or 0
        return counts
    finally:
        await database.dispose()


async def summarise(database_url: str | None) -> dict[str, int]:
    settings = Settings(database_url=database_url) if database_url else Settings()
    database = Database(settings)
    try:
        async with database.session() as session:
            total_runs = (await session.execute(select(func.count()).select_from(tables.RunRow))).scalar_one()
            total_events = (
                await session.execute(select(func.count()).select_from(tables.TraceEventRow))
            ).scalar_one()
            pending = (
                await session.execute(
                    select(func.count())
                    .select_from(tables.ApprovalRow)
                    .where(tables.ApprovalRow.status == "pending")
                )
            ).scalar_one()
        return {"runs": int(total_runs), "trace_events": int(total_events), "pending_approvals": int(pending)}
    finally:
        await database.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=90, help="retention window in days (default 90)")
    parser.add_argument("--db", default=None, help="database URL (default: AO_DATABASE_URL)")
    parser.add_argument("--dry-run", action="store_true", help="report what would be deleted")
    parser.add_argument("--batch", type=int, default=5000, help="maximum runs per invocation")
    parser.add_argument("--stats", action="store_true", help="print current ledger size and exit")
    args = parser.parse_args(argv)

    database_url = args.db or os.environ.get("AO_DATABASE_URL")
    if args.days < 1:
        print("--days must be >= 1", file=sys.stderr)
        return 1

    try:
        if args.stats:
            print(summarise_sync(database_url))
            return 0
        counts = asyncio.run(
            prune(database_url=database_url, days=args.days, dry_run=args.dry_run, batch=args.batch)
        )
    except Exception as exc:  # pragma: no cover - operational script
        print(f"prune failed: {exc}", file=sys.stderr)
        return 1

    scope = "would delete" if args.dry_run else "deleted"
    print(
        f"{scope}: {counts.get('runs', 0)} run(s), {counts.get('trace_events', 0)} trace event(s), "
        f"{counts.get('policy_decisions', 0)} policy decision(s), {counts.get('approvals', 0)} approval(s) "
        f"older than {args.days} day(s)"
        + (f" (candidates: {counts.get('candidates', 0)})" if args.dry_run else "")
    )
    return 0


def summarise_sync(database_url: str | None) -> dict[str, int]:
    return asyncio.run(summarise(database_url))


if __name__ == "__main__":
    raise SystemExit(main())
