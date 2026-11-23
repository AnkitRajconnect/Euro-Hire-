#!/usr/bin/env python3
"""Daily refresh of France jobs from discovered public ATS boards."""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import httpx

from job_pipeline import (
    PROVIDERS, HostRateLimiter, create_database, export_active_jobs,
    fetch_board_jobs, read_active_sources, record_source_check,
    upsert_jobs_for_source, utc_now,
)


async def run_sync(db_path="data/jobs.db", output_dir="data", report_path="data/sync_report.json"):
    checked_at = utc_now()
    connection = create_database(db_path)
    sources = read_active_sources(connection)
    report: dict[str, Any] = {
        "generated_at": checked_at, "sources_total": len(sources),
        "sources_succeeded": 0, "sources_failed": 0, "jobs_seen": 0,
        "jobs_active": 0, "expired_jobs": 0, "providers": {}, "errors": [],
    }
    if not sources:
        connection.close()
        raise RuntimeError("No ATS sources found. Run weekly company discovery first.")

    headers = {"User-Agent": "EuroHireDailySync/1.0 (+https://github.com/AnkitRajconnect/Euro-Hire-)"}
    timeout = httpx.Timeout(35.0, connect=15.0)
    limits = httpx.Limits(max_connections=40, max_keepalive_connections=20)
    limiter = HostRateLimiter()
    semaphores = {
        provider: asyncio.Semaphore(int(config["concurrency"]))
        for provider, config in PROVIDERS.items()
    }
    async with httpx.AsyncClient(timeout=timeout, limits=limits, follow_redirects=True, headers=headers) as client:
        async def sync_one(source: sqlite3.Row):
            provider = source["ats_type"]
            async with semaphores.get(provider, asyncio.Semaphore(4)):
                success, jobs, error = await fetch_board_jobs(
                    provider, source["ats_slug"], client, limiter
                )
            stats = report["providers"].setdefault(
                provider, {"sources_ok": 0, "sources_failed": 0, "jobs": 0}
            )
            if success:
                before = connection.execute(
                    "SELECT COUNT(*) FROM jobs WHERE source_id=? AND is_active=1",
                    (source["source_id"],),
                ).fetchone()[0]
                upsert_jobs_for_source(connection, source["source_id"], jobs, checked_at)
                after = connection.execute(
                    "SELECT COUNT(*) FROM jobs WHERE source_id=? AND is_active=1",
                    (source["source_id"],),
                ).fetchone()[0]
                report["sources_succeeded"] += 1
                report["jobs_seen"] += len(jobs)
                report["expired_jobs"] += max(0, int(before) - int(after))
                stats["sources_ok"] += 1
                stats["jobs"] += len(jobs)
                record_source_check(connection, source["source_id"], checked_at, True)
            else:
                report["sources_failed"] += 1
                stats["sources_failed"] += 1
                record_source_check(connection, source["source_id"], checked_at, False, error)
                report["errors"].append({
                    "provider": provider, "slug": source["ats_slug"],
                    "error": (error or "unknown error")[:240],
                })

        await asyncio.gather(*(sync_one(source) for source in sources))
        connection.commit()

    report["jobs_active"] = int(connection.execute(
        "SELECT COUNT(*) FROM jobs WHERE is_active=1"
    ).fetchone()[0])

    if report["sources_succeeded"] == 0:
        connection.close()
        report_file = Path(report_path)
        report_file.parent.mkdir(parents=True, exist_ok=True)
        report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        raise RuntimeError("Every ATS feed failed; previous JSON and CSV exports were preserved.")

    export_active_jobs(connection, output_dir)
    connection.close()
    report_file = Path(report_path)
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"Checked {report['sources_succeeded']}/{report['sources_total']} sources; "
        f"{report['jobs_active']} active France jobs."
    )
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Sync live France jobs from public ATS feeds.")
    parser.add_argument("--db", default="data/jobs.db")
    parser.add_argument("--output-dir", default="data")
    parser.add_argument("--report", default="data/sync_report.json")
    args = parser.parse_args(argv)
    try:
        asyncio.run(run_sync(args.db, args.output_dir, args.report))
    except Exception as exc:
        print(f"Daily sync failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
