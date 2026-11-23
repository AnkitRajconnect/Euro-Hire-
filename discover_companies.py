#!/usr/bin/env python3
"""Weekly ATS-first discovery for Europe.

1. Seed public ATS board tokens from an MIT-licensed public snapshot.
2. Expand the board index from Common Crawl S3 range indexes.
3. Fetch live public job feeds and keep only Europe-based vacancies.
4. Enrich companies with SIREN/NAF after jobs have proved the board is relevant.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from ats_pipeline import (
    DB_DEFAULT, PoliteHTTP, enrich_company_insee, fetch_ats_board,
    fetch_wttj_europe_jobs, init_db, mark_board_error, store_board_result,
    utc_now, write_exports,
)
from commoncrawl_index import harvest_commoncrawl, seed_from_open_ats_feed

PROVIDER_LIMITS = {
    "greenhouse": 6, "lever": 1, "ashby": 6, "workable": 1,
    "breezy": 6, "recruitee": 6, "teamtailor": 6, "taleez": 4, "smartrecruiters": 1,
}


async def probe_board(http, conn, row, semaphores, run_started):
    board = dict(row)
    provider = board["ats_type"]
    async with semaphores[provider]:
        raw_jobs, error = await fetch_ats_board(http, board)
    if error:
        mark_board_error(conn, board, error, run_started)
        conn.commit()
        return {"ats": provider, "board": board["ats_slug"], "ok": False, "jobs": 0, "error": error}
    kept = store_board_result(conn, board, raw_jobs, run_started)
    conn.commit()
    return {"ats": provider, "board": board["ats_slug"], "ok": True,
            "raw": len(raw_jobs), "fr_jobs": kept}


async def sync_wttj(http, conn, run_started):
    """WTTJ is one country-filtered global index, not a list of company boards."""
    raw_jobs, error = await fetch_wttj_europe_jobs(http)
    if error:
        conn.execute("""
          INSERT INTO source_runs(source_key,last_checked_at,last_error)
          VALUES('wttj-europe',?,?)
          ON CONFLICT(source_key) DO UPDATE SET
            last_checked_at=excluded.last_checked_at,last_error=excluded.last_error
        """, (run_started, error[:500]))
        conn.commit()
        return {"source": "wttj", "ok": False, "error": error, "fr_jobs": 0}
    kept = store_board_result(
        conn,
        {"ats_type": "wttj", "ats_slug": "europe-index"},
        raw_jobs, run_started, deactivate=False, update_board=False,
    )
    # Only a complete successful global search is allowed to expire older WTTJ jobs.
    conn.execute("""
      UPDATE jobs SET is_active=0
      WHERE source_type='wttj' AND last_seen_at < ?
    """, (run_started,))
    conn.execute("""
      INSERT INTO source_runs(source_key,last_checked_at,last_success_at,last_error)
      VALUES('wttj-europe',?,?,NULL)
      ON CONFLICT(source_key) DO UPDATE SET
        last_checked_at=excluded.last_checked_at,
        last_success_at=excluded.last_success_at,last_error=NULL
    """, (run_started, utc_now()))
    conn.commit()
    return {"source": "wttj", "ok": True, "raw": len(raw_jobs), "fr_jobs": kept}


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
        "pipeline": "weekly_ats_first_discovery",
        "company_discovery": "public ATS board tokens before INSEE enrichment",
        "seed_boards_added": 0,
        "commoncrawl_board_ids_added": 0,
        "boards_selected": 0,
        "boards_ok": 0,
        "boards_failed": 0,
        "europe_jobs_from_ats": 0,
        "wttj": None,
        "insee_companies_checked": 0,
    }
    try:
        if not args.skip_seed:
            report["seed_boards_added"] = await seed_from_open_ats_feed(http, conn)
        if not args.skip_commoncrawl:
            report["commoncrawl_board_ids_added"] = await harvest_commoncrawl(
                http, conn, crawl_limit=args.crawl_limit
            )

        # New boards are probed first. Boards that previously had no European jobs
        # are reconsidered quarterly; active European boards are checked daily.
        rows = conn.execute("""
          SELECT * FROM ats_boards
          WHERE last_probed_at IS NULL
             OR (last_error IS NOT NULL AND last_probed_at < datetime('now','-1 day'))
             OR (last_error IS NULL AND last_probed_at < datetime('now','-90 days'))
          ORDER BY CASE WHEN last_probed_at IS NULL THEN 0
                        WHEN last_error IS NOT NULL THEN 1 ELSE 2 END,
                   COALESCE(seed_posting_count,0) DESC,
                   first_seen_at
          LIMIT ?
        """, (args.max_boards,)).fetchall()
        report["boards_selected"] = len(rows)
        semaphores = {
            provider: asyncio.Semaphore(PROVIDER_LIMITS[provider])
            for provider in PROVIDER_LIMITS
        }
        results = await asyncio.gather(*(
            probe_board(http, conn, row, semaphores, run_started) for row in rows
        ))
        report["boards_ok"] = sum(item["ok"] for item in results)
        report["boards_failed"] = len(results) - report["boards_ok"]
        report["europe_jobs_from_ats"] = sum(item.get("fr_jobs", 0) for item in results)

        report["wttj"] = await sync_wttj(http, conn, run_started)
        report["insee_companies_checked"] = await enrich_pending(http, conn, args.max_insee)

        report["companies_with_europe_jobs"] = conn.execute(
            "SELECT COUNT(*) FROM companies"
        ).fetchone()[0]
        report["companies_matched_to_insee"] = conn.execute(
            "SELECT COUNT(*) FROM companies WHERE insee_match_status='matched'"
        ).fetchone()[0]
        report["active_europe_jobs"] = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE is_active=1"
        ).fetchone()[0]
        report["boards_with_europe_jobs"] = conn.execute(
            "SELECT COUNT(*) FROM ats_boards WHERE has_europe_jobs=1"
        ).fetchone()[0]
        report["boards_remaining_unprobed"] = conn.execute(
            "SELECT COUNT(*) FROM ats_boards WHERE last_probed_at IS NULL"
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
            "companies": report["companies_with_europe_jobs"],
            "insee_matched": report["companies_matched_to_insee"],
            "unprobed": report["boards_remaining_unprobed"],
        }, ensure_ascii=False))
        return 0 if report["boards_ok"] or report["wttj"]["ok"] else 2
    finally:
        await http.close()
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB_DEFAULT)
    parser.add_argument("--max-boards", type=int, default=10000)
    parser.add_argument("--crawl-limit", type=int, default=3)
    parser.add_argument("--max-insee", type=int, default=5000)
    parser.add_argument("--timeout", type=float, default=25)
    parser.add_argument("--skip-seed", action="store_true")
    parser.add_argument("--skip-commoncrawl", action="store_true")
    args = parser.parse_args()
    if args.max_boards < 1 or args.max_insee < 0 or args.crawl_limit < 1:
        parser.error("limits must be positive (max-insee may be zero)")
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
