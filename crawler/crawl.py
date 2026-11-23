"""Daily crawl of reviewed public employer career boards.

    python -m crawler.crawl                 # crawl the reviewed board list
    python -m crawler.crawl --only spotify  # crawl one employer/source

Output:
    jobs.json    active Europe-eligible jobs shown by the website
    report.json  source-by-source crawl result
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .adapters import ADAPTERS
from .company_security import is_company_approved, screen_vacancy
from .http import FetchError, Http
from .normalize import normalize

MAX_STALE_DAYS = 3


def load_companies(path: Path, only: set[str] | None = None) -> list[dict]:
    """Load the reviewed employer-owned career boards from companies.yaml."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    candidates = data.get("companies", [])
    out, seen = [], set()
    for item in candidates:
        if not isinstance(item, dict):
            continue
        company = dict(item)
        company.setdefault("name", company.get("slug", "Unnamed company"))
        company.setdefault("ats_slug", company.get("slug"))
        if not company.get("verification"):
            reviewed = company.get("source_reviewed_on")
            company["verification"] = {
                "mode": "curated_public_ats",
                "status": "approved",
                "domain": {
                    "url": "https://" + str(company.get("official_domain", "")),
                    "verified": True,
                    "method": "public_ats_board_review",
                },
                "source": {
                    "approved": True,
                    "verified": True,
                    "method": "public_ats_board_review",
                    "career_url": company.get("career_url"),
                    "evidence_url": company.get("official_careers_url"),
                    "checked_at": reviewed,
                },
            }
        key = (company.get("ats"), company.get("ats_slug"))
        if key in seen:
            continue
        seen.add(key)
        ok, reason = is_company_approved(company)
        if not ok:
            print(f"! blocked {company['name']}: {reason}", file=sys.stderr)
            continue
        if company.get("ats") not in ADAPTERS:
            print(f"! unknown ATS '{company.get('ats')}' for {company['name']}", file=sys.stderr)
            continue
        if only and company.get("slug", "").lower() not in only and company.get("ats_slug", "").lower() not in only and company["name"].lower() not in only:
            continue
        out.append(company)
    return out


def crawl_company(company: dict, http: Http, now_iso: str) -> dict:
    result = {"company": company["name"], "slug": company.get("slug") or company.get("ats_slug"),
              "ats": company["ats"], "ok": False, "raw": 0, "kept": 0, "rejected": 0,
              "review": 0, "reasons": {}, "error": None, "jobs": [], "review_items": []}
    approved, reason = is_company_approved(company)
    if not approved:
        result["error"] = f"source approval blocked: {reason}"
        return result
    try:
        options = {key: company[key] for key in ("eu", "urls") if key in company}
        raw_jobs = ADAPTERS[company["ats"]](company.get("ats_slug") or company["slug"], http, **options)
    except FetchError as exc:
        result["error"] = str(exc)
        return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    result["ok"] = True
    result["raw"] = len(raw_jobs)
    for raw in raw_jobs:
        if not raw.get("id"):
            continue
        decision, reasons = screen_vacancy(raw, company)
        if decision != "publish":
            result["rejected" if decision == "reject" else "review"] += 1
            for reason in reasons:
                result["reasons"][reason] = result["reasons"].get(reason, 0) + 1
            if decision == "review":
                result["review_items"].append({
                    "company": company["name"], "company_slug": company.get("slug"),
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


def merge(previous: list[dict], results: list[dict], now: datetime) -> list[dict]:
    """Drop closed jobs; retain jobs briefly when an employer feed is down."""
    today = now.date().isoformat()
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
        if job_id in merged or old.get("company_slug") in working_feeds:
            continue
        try:
            age = (now.date() - datetime.fromisoformat(old.get("last_seen", today)).date()).days
        except ValueError:
            age = MAX_STALE_DAYS + 1
        if age <= MAX_STALE_DAYS:
            merged[job_id] = old
    return sorted(merged.values(), key=lambda job: (job.get("posted") or job.get("first_seen") or ""), reverse=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--companies", default="companies.yaml")
    parser.add_argument("--review-out", default="/tmp/eurohire-review-queue.json")
    parser.add_argument("--out", default="site/data")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--only", default="")
    parser.add_argument("--verify", action="store_true", help="check feeds without writing output")
    args = parser.parse_args(argv)

    now = datetime.now(timezone.utc)
    now_iso = now.date().isoformat()
    only = {part.strip().lower() for part in args.only.split(",") if part.strip()} or None
    companies = load_companies(Path(args.companies), only)
    if not companies:
        print("No reviewed public employer boards are ready to crawl. Check companies.yaml.", file=sys.stderr)
        return 2

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
                status = f"FAILED  {result['error']}"
            print(f"{result['company']:<28} {result['ats']:<16} {status}")

    feeds_ok = sum(result["ok"] for result in results)
    print(f"\n{feeds_ok}/{len(results)} feeds ok; {sum(r['kept'] for r in results)} Europe jobs published")
    if args.verify:
        return 0

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    report = {
        "generated_at": now.isoformat(timespec="seconds"),
        "feeds_ok": feeds_ok,
        "feeds_total": len(results),
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

    # Fail closed on a total upstream outage.
    if feeds_ok == 0:
        print("All employer feeds failed. Keeping the last successful jobs.json.", file=sys.stderr)
        return 2

    jobs_path = out / "jobs.json"
    previous = []
    if jobs_path.exists():
        try:
            payload = json.loads(jobs_path.read_text(encoding="utf-8"))
            previous = [] if payload.get("demo") else payload.get("jobs", [])
        except (ValueError, AttributeError):
            previous = []
    approved_slugs = {company.get("slug") or company.get("ats_slug") for company in companies}
    previous = [job for job in previous if job.get("company_slug") in approved_slugs]
    jobs = merge(previous, results, now)
    payload = {"generated_at": now.isoformat(timespec="seconds"), "count": len(jobs), "demo": False, "jobs": jobs}
    jobs_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {jobs_path} ({len(jobs)} active jobs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
