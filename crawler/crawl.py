"""Crawl public employer ATS boards found by the dynamic board index."""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from .adapters import ADAPTERS
from .company_security import is_company_approved, screen_vacancy
from .http import FetchError, Http
from .normalize import normalize

MAX_STALE_DAYS = 14


def load_companies(path: Path, only: set[str] | None = None) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    candidates = payload.get("boards", []) if isinstance(payload, dict) else []
    out, seen = [], set()
    for item in candidates:
        if not isinstance(item, dict):
            continue
        company = dict(item)
        company.setdefault("name", str(company.get("slug") or "employer").replace("-", " ").title())
        company.setdefault("ats_slug", company.get("slug"))
        company.setdefault("company_key", f"{company.get('ats')}:{company.get('ats_slug')}")
        company["verification"] = {
            "mode": "discovered_public_ats", "status": "approved",
            "source": {"board_url": company.get("board_url"),
                       "discovery_source": payload.get("source", "Common Crawl URL index")},
        }
        key = (company.get("ats"), company.get("ats_slug"))
        if key in seen or company.get("ats") not in ADAPTERS:
            continue
        seen.add(key)
        if not is_company_approved(company)[0]:
            continue
        if only and company.get("ats_slug", "").lower() not in only and company.get("name", "").lower() not in only:
            continue
        out.append(company)
    return out


def crawl_company(company: dict, http: Http, now_iso: str) -> dict:
    source_key = company["company_key"]
    result = {
        "company": company["name"], "slug": source_key, "ats": company["ats"],
        "board_id": company["ats_slug"], "ok": False, "raw": 0, "kept": 0,
        "rejected": 0, "review": 0, "reasons": {}, "error": None, "jobs": [],
        "review_items": [],
    }
    if not is_company_approved(company)[0]:
        result["error"] = "public ATS board did not pass validation"
        return result
    try:
        options = {key: company[key] for key in ("eu", "urls") if key in company}
        raw_jobs = ADAPTERS[company["ats"]](company["ats_slug"], http, **options)
    except FetchError as error:
        result["error"] = str(error)
        return result
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
        return result

    result["ok"] = True
    result["raw"] = len(raw_jobs)
    for raw in raw_jobs:
        if not raw.get("id"):
            continue
        # The board ID/host was checked before this public feed was called.
        # Its returned HTTPS apply link may live on the employer's own domain.
        raw["_source_feed_validated"] = True
        raw["company_name"] = raw.get("company_name") or company["name"]
        decision, reasons = screen_vacancy(raw, company)
        if decision != "publish":
            result["rejected" if decision == "reject" else "review"] += 1
            for reason in reasons:
                result["reasons"][reason] = result["reasons"].get(reason, 0) + 1
            if decision == "review":
                result["review_items"].append({
                    "company": company["name"], "company_slug": source_key,
                    "title": (raw.get("title") or "")[:180], "url": raw.get("url"),
                    "reason": reasons, "ats": company["ats"], "checked_at": now_iso,
                })
            continue
        job = normalize(raw, company, now_iso)
        if job:
            result["jobs"].append(job)
        else:
            result["rejected"] += 1
            reason = "outside Europe or missing/invalid location"
            result["reasons"][reason] = result["reasons"].get(reason, 0) + 1
    result["kept"] = len(result["jobs"])
    return result


def merge(previous: list[dict], results: list[dict], now: datetime,
          not_crawled: set[str] | None = None) -> list[dict]:
    today = now.date().isoformat()
    not_crawled = not_crawled or set()
    old_by_id = {job["id"]: job for job in previous}
    working_feeds = {result["slug"] for result in results if result["ok"]}
    merged = {}
    for result in results:
        for job in result["jobs"]:
            old = old_by_id.get(job["id"])
            if old:
                job["first_seen"] = old.get("first_seen", today)
            merged[job["id"]] = job
    for job_id, old in old_by_id.items():
        if job_id in merged:
            continue
        source_key = old.get("company_slug")
        if source_key in not_crawled:
            merged[job_id] = old
            continue
        if source_key in working_feeds:
            continue
        try:
            age = (now.date() - datetime.fromisoformat(old.get("last_seen", today)).date()).days
        except (TypeError, ValueError):
            age = MAX_STALE_DAYS + 1
        if age <= MAX_STALE_DAYS:
            merged[job_id] = old
    return sorted(merged.values(), key=lambda job: (job.get("posted") or job.get("first_seen") or ""), reverse=True)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--boards", default="site/data/boards.json")
    parser.add_argument("--review-out", default="/tmp/eurohire-review-queue.json")
    parser.add_argument("--out", default="site/data")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--max-boards", type=int, default=5000)
    parser.add_argument("--only", default="")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args(argv)

    now = datetime.now(timezone.utc)
    now_iso = now.date().isoformat()
    only = {part.strip().lower() for part in args.only.split(",") if part.strip()} or None
    boards_path = Path(args.boards)
    board_payload = json.loads(boards_path.read_text(encoding="utf-8"))
    all_companies = load_companies(boards_path, only)
    if not all_companies:
        print("No public ATS boards found. Run source discovery first.", file=sys.stderr)
        return 2

    ordered = sorted(all_companies, key=lambda item: (
        item.get("last_crawled") or "0000-00-00", item["ats"], item["ats_slug"]))
    companies = ordered[:max(1, args.max_boards)]
    selected = {company["company_key"] for company in companies}
    all_keys = {company["company_key"] for company in all_companies}
    not_crawled = all_keys - selected

    http = Http(min_delay=0.4)
    results = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = [executor.submit(crawl_company, company, http, now_iso) for company in companies]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            status = (f"{result['kept']:>4} kept / {result['raw']:>4} raw / "
                      f"{result['rejected']:>3} rejected / {result['review']:>3} review")
            if not result["ok"]:
                status = f"FAILED {result['error']}"
            print(f"{result['company']:<28} {result['ats']:<16} {status}")

    feeds_ok = sum(result["ok"] for result in results)
    print(f"\n{feeds_ok}/{len(results)} feeds ok; {sum(item['kept'] for item in results)} Europe jobs published")
    if args.verify:
        return 0

    result_by_key = {(item["ats"], item["board_id"]): item for item in results}
    for board in board_payload.get("boards", []):
        key = (board.get("ats"), board.get("ats_slug") or board.get("slug"))
        result = result_by_key.get(key)
        if result:
            board["last_crawled"] = now_iso
            if result["ok"]:
                board["last_successful"] = now_iso
                board.pop("last_error", None)
            else:
                board["last_error"] = (result.get("error") or "")[:240]
    board_payload["last_crawl_at"] = now.isoformat(timespec="seconds")
    boards_path.write_text(json.dumps(board_payload, ensure_ascii=False, indent=1), encoding="utf-8")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    by_ats = {}
    for company in all_companies:
        by_ats[company["ats"]] = by_ats.get(company["ats"], 0) + 1
    report = {
        "generated_at": now.isoformat(timespec="seconds"),
        "source_boards_total": len(all_companies),
        "source_boards_crawled": len(results),
        "source_boards_remaining": len(not_crawled),
        "ats_counts": by_ats,
        "feeds_ok": feeds_ok, "feeds_total": len(results),
        "jobs_published": sum(result["kept"] for result in results),
        "jobs_rejected": sum(result["rejected"] for result in results),
        "jobs_held_for_review": sum(result["review"] for result in results),
        "companies": sorted(
            ({key: value for key, value in result.items() if key not in {"jobs", "review_items"}}
             for result in results),
            key=lambda item: (item["ok"], item["company"]),
        ),
    }
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    review_items = [item for result in results for item in result["review_items"]]
    review_path = Path(args.review_out)
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(json.dumps({
        "generated_at": now.isoformat(timespec="seconds"),
        "count": len(review_items), "items": review_items,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    if feeds_ok == 0:
        print("All checked ATS feeds failed; preserving the previous jobs.json.", file=sys.stderr)
        return 2

    jobs_path = out / "jobs.json"
    previous = []
    if jobs_path.exists():
        try:
            old_payload = json.loads(jobs_path.read_text(encoding="utf-8"))
            previous = [] if old_payload.get("demo") else old_payload.get("jobs", [])
        except (ValueError, AttributeError):
            previous = []
    previous = [job for job in previous if job.get("company_slug") in all_keys]
    jobs = merge(previous, results, now, not_crawled=not_crawled)
    payload = {"generated_at": now.isoformat(timespec="seconds"),
               "count": len(jobs), "demo": False, "jobs": jobs}
    jobs_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {jobs_path} ({len(jobs)} jobs; {len(not_crawled)} boards queued)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
