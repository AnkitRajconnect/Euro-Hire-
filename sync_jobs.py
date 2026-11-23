#!/usr/bin/env python3
"""Daily France ATS job sync.

Reads only previously discovered boards that have produced French vacancies.
A successful complete board response expires missing jobs; failed feeds do not.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from ats_pipeline import (
    DB_DEFAULT, PoliteHTTP, enrich_company_insee, fetch_ats_board,
    fetch_wttj_france_jobs, init_db, mark_board_error, store_board_result,
    utc_now, write_exports,
)

PROVIDER_LIMITS = {
    "greenhouse": 6, "lever": 1, "ashby": 6, "workable": 1,
    "breezy": 6, "recruitee": 6, "teamtailor": 6, "taleez": 4, "smartrecruiters": 1,
}


async def sync_board(http, conn, row, semaphores, run_started):
    board = dict(row)
    provider = board["ats_type"]
    async with semaphores[provider]:
        raw_jobs, error = await fetch_ats_board(http, board)
    if error:
        mark_board_error(conn, board, error, run_started)
        conn.commit()
        return {"ats": provider, "ok": False, "fr_jobs": 0, "error": error}
    kept = store_board_result(conn, board, raw_jobs, run_started)
    conn.commit()
    return {"ats": provider, "ok": True, "raw": len(raw_jobs), "fr_jobs": kept}


async def sync_wttj(http, conn, run_started):
    raw_jobs, error = await fetch_wttj_france_jobs(http)
    if error:
        conn.execute("""
          INSERT INTO source_runs(source_key,last_checked_at,last_error)
          VALUES('wttj-france',?,?)
          ON CONFLICT(source_key) DO UPDATE SET
            last_checked_at=excluded.last_checked_at,last_error=excluded.last_error
        """, (run_started, error[:500]))
        conn.commit()
        return {"ok": False, "error": error, "fr_jobs": 0}
    kept = store_board_result(
        conn, {"ats_type": "wttj", "ats_slug": "france-index"},
        raw_jobs, run_started, deactivate=False, update_board=False,
    )
    conn.execute("""
      UPDATE jobs SET is_active=0
      WHERE source_type='wttj' AND last_seen_at < ?
    """, (run_started,))
    conn.execute("""
      INSERT INTO source_runs(source_key,last_checked_at,last_success_at,last_error)
      VALUES('wttj-france',?,?,NULL)
      ON CONFLICT(source_key) DO UPDATE SET
        last_checked_at=excluded.last_checked_at,
        last_success_at=excluded.last_success_at,last_error=NULL
    """, (run_started, utc_now()))
    conn.commit()
    return {"ok": True, "raw": len(raw_jobs), "fr_jobs": kept}


async def enrich_pending(http, conn, limit: int):
    rows = conn.execute("""
      SELECT company_key FROM companies
      WHERE insee_match_status IN ('pending','error')
         OR (insee_match_status='unmatched'
             AND (insee_checked_at IS NULL OR insee_checked_at < datetime('now','-30 days')))
      ORDER BY CASE insee_match_status WHEN 'pending' THEN 0 WHEN 'error' THEN 1 ELSE 2 END,
               first_seen_at
      LIMIT ?
    """, (limit,)).fetchall()
    semaphore = asyncio.Semaphore(6)

    async def one(row):
        async with semaphore:
            return await enrich_company_insee(http, conn, row["company_key"])

    if rows:
        await asyncio.gather(*(one(row) for row in rows))
        conn.commit()
    return len(rows)


async def run(args):
    conn = init_db(args.db)
    http = PoliteHTTP(timeout=args.timeout)
    run_started = utc_now()
    report = {
        "generated_at": run_started,
        "pipeline": "daily_ats_job_sync",
        "boards_selected": 0, "boards_ok": 0, "boards_failed": 0,
        "france_jobs_from_ats": 0, "wttj": None, "insee_companies_checked": 0,
    }
    try:
        query = """
          SELECT * FROM ats_boards WHERE has_fr_jobs=1
          ORDER BY COALESCE(last_probed_at,'0000-00-00'), ats_type, ats_slug
        """
        rows = conn.execute(query).fetchall()
        if args.max_boards:
            rows = rows[:args.max_boards]
        report["boards_selected"] = len(rows)
        semaphores = {
            provider: asyncio.Semaphore(PROVIDER_LIMITS[provider])
            for provider in PROVIDER_LIMITS
        }
        results = await asyncio.gather(*(
            sync_board(http, conn, row, semaphores, run_started) for row in rows
        ))
        report["boards_ok"] = sum(item["ok"] for item in results)
        report["boards_failed"] = len(results) - report["boards_ok"]
        report["france_jobs_from_ats"] = sum(item.get("fr_jobs", 0) for item in results)
        report["wttj"] = await sync_wttj(http, conn, run_started)
        report["insee_companies_checked"] = await enrich_pending(http, conn, args.max_insee)

        report["active_france_jobs"] = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE is_active=1"
        ).fetchone()[0]
        report["companies_count"] = conn.execute(
            "SELECT COUNT(*) FROM companies"
        ).fetchone()[0]
        report["companies_matched_to_insee"] = conn.execute(
            "SELECT COUNT(*) FROM companies WHERE insee_match_status='matched'"
        ).fetchone()[0]
        report["boards_with_fr_jobs"] = conn.execute(
            "SELECT COUNT(*) FROM ats_boards WHERE has_fr_jobs=1"
        ).fetchone()[0]
        report["ats_board_counts"] = {
            row["ats_type"]: row["n"] for row in conn.execute(
                "SELECT ats_type,COUNT(*) AS n FROM ats_boards GROUP BY ats_type"
            )
        }
        conn.commit()
        data = write_exports(conn, report=report)
        print(json.dumps({
            "boards_selected": report["boards_selected"],
            "boards_ok": report["boards_ok"],
            "france_jobs": data["count"],
            "companies": report["companies_count"],
            "insee_matched": report["companies_matched_to_insee"],
        }, ensure_ascii=False))
        return 0 if report["boards_ok"] or report["wttj"]["ok"] else (0 if not rows else 2)
    finally:
        await http.close()
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB_DEFAULT)
    parser.add_argument("--max-boards", type=int, default=0,
                        help="0 means all discovered French boards")
    parser.add_argument("--max-insee", type=int, default=3000)
    parser.add_argument("--timeout", type=float, default=25)
    args = parser.parse_args()
    if args.max_boards < 0 or args.max_insee < 0:
        parser.error("limits cannot be negative")
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
