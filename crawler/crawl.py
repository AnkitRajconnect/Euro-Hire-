"""Daily crawl of approved company career sources.

    python -m crawler.crawl                      # read local registry or approved snapshot
    python -m crawler.crawl --only fr-123456789  # crawl one approved company/source
    python -m crawler.crawl --verify             # just report which feeds respond

Output (in --out, default site/data):
    jobs.json     active European jobs (what the website reads)
    report.json   per-company status, counts, errors (look here when something breaks)
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
from .company_registry import DEFAULT_DB, approved_companies
from .company_security import is_company_approved, screen_vacancy
from .http import FetchError, Http
from .normalize import normalize

MAX_STALE_DAYS = 3  # keep jobs of a failing company this long before dropping them


def load_companies(path: Path, only: set[str] | None = None, registry_db: Path | None = DEFAULT_DB,
                   approved_file: Path = Path("companies-approved.json"),
                   curated_config: bool = False) -> list[dict]:
    """Load approved sources from SQLite, an approval snapshot, or the curated launch list."""
    if curated_config:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        candidates = data.get("companies", [])
        # Dynamically add every approved French legal-entity source from SIRENE.
        # The YAML entries are compatibility seeds, not a company-count limit.
        if registry_db is not None and registry_db.exists():
            candidates = list(candidates) + [
                c for c in approved_companies(registry_db) if c.get("hq") == "FR"
            ]
        for item in candidates:
            item.setdefault("ats_slug", item.get("slug"))
            reviewed = item.get("source_reviewed_on")
            item["verification"] = {
                "mode": "curated_public_ats",
                "status": "approved",
                "domain": {
                    "url": "https://" + str(item.get("official_domain", "")),
                    "verified": True,
                    "method": "public_ats_board_review",
                },
                "source": {
                    "approved": True,
                    "verified": True,
                    "method": "public_ats_board_review",
                    "career_url": item.get("career_url"),
                    "evidence_url": item.get("official_careers_url"),
                    "checked_at": reviewed,
                },
            }
    elif registry_db is not None and registry_db.exists():
        candidates = approved_companies(registry_db)
    elif approved_file.exists():
        payload = json.loads(approved_file.read_text(encoding="utf-8"))
        candidates = payload.get("companies", []) if isinstance(payload, dict) else []
    else:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        candidates = data.get("companies", [])

    out, seen = [], set()
    for c in candidates:
        c = dict(c)
        c.setdefault("name", c.get("slug", "Unnamed company"))
        c.setdefault("ats_slug", c.get("slug"))
        key = (c.get("ats"), c.get("ats_slug"))
        if key in seen:
            continue
        seen.add(key)
        ok, reason = is_company_approved(c)
        if not ok:
            print(f"! blocked {c['name']}: {reason}", file=sys.stderr)
            continue
        if c["ats"] not in ADAPTERS:
            print(f"! unknown ats '{c['ats']}' for {c['name']}", file=sys.stderr)
            continue
        if only and c.get("slug", "").lower() not in only and c.get("ats_slug", "").lower() not in only and c["name"].lower() not in only:
            continue
        out.append(c)
    return out


def crawl_company(c: dict, http: Http, now_iso: str) -> dict:
    res = {"company": c["name"], "slug": c.get("slug") or c.get("ats_slug"), "ats": c["ats"], "ok": False,
           "raw": 0, "kept": 0, "rejected": 0, "review": 0, "reasons": {}, "error": None, "jobs": [], "review_items": []}
    approved, why = is_company_approved(c)
    if not approved:
        res["error"] = f"publication gate blocked: {why}"
        return res
    feed_slug = c.get("ats_slug") or c["slug"]
    try:
        opts = {k: c[k] for k in ("eu", "urls") if k in c}
        raw_jobs = ADAPTERS[c["ats"]](feed_slug, http, **opts)
    except FetchError as e:
        res["error"] = str(e)
        return res
    except Exception as e:  # a broken adapter must never kill the whole run
        res["error"] = f"{type(e).__name__}: {e}"
        return res
    res["ok"] = True
    res["raw"] = len(raw_jobs)
    for r in raw_jobs:
        if not r.get("id"):
            continue
        decision, reasons = screen_vacancy(r, c)
        if decision != "publish":
            res["rejected" if decision == "reject" else "review"] += 1
            for reason in reasons:
                res["reasons"][reason] = res["reasons"].get(reason, 0) + 1
            if decision == "review":
                res["review_items"].append({
                    "company": c["name"], "company_slug": c.get("slug"),
                    "title": (r.get("title") or "")[:180], "url": r.get("url"),
                    "reason": reasons, "ats": c["ats"], "checked_at": now_iso,
                })
            continue
        job = normalize(r, c, now_iso)
        if job:
            res["jobs"].append(job)
        else:
            res["rejected"] += 1
            res["reasons"]["outside Europe or occupation not in launch taxonomy"] = res["reasons"].get("outside Europe or occupation not in launch taxonomy", 0) + 1
    res["kept"] = len(res["jobs"])
    return res


def merge(previous: list[dict], results: list[dict], now: datetime) -> list[dict]:
    """Jobs that vanished from a successfully-read feed are closed (dropped).
    Jobs of a company whose feed failed today are kept for a few days, so one
    bad morning doesn't wipe the board."""
    now_iso = now.date().isoformat()
    prev = {j["id"]: j for j in previous}
    ok_companies = {r["slug"] for r in results if r["ok"]}
    merged: dict[str, dict] = {}
    for r in results:
        for j in r["jobs"]:
            old = prev.get(j["id"])
            if old:
                j["first_seen"] = old.get("first_seen", now_iso)
            merged[j["id"]] = j
    for jid, old in prev.items():
        if jid in merged or old.get("company_slug") in ok_companies:
            continue
        age = (now.date() - datetime.fromisoformat(old.get("last_seen", now_iso)).date()).days
        if age <= MAX_STALE_DAYS:
            merged[jid] = old
    return sorted(merged.values(), key=lambda j: (j.get("posted") or j["first_seen"]), reverse=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--companies", default="companies.yaml")
    ap.add_argument("--registry-db", default=str(DEFAULT_DB), help="Local SQLite registry database; if absent, use --approved-companies snapshot")
    ap.add_argument("--approved-companies", default="companies-approved.json", help="Small JSON snapshot of registry-approved sources")
    ap.add_argument("--curated-config", action="store_true", help="Use the included first-party public ATS source list; no registry database or API key needed")
    ap.add_argument("--review-out", default="data/review_queue.json", help="Write held vacancies here (keep this file out of the public site)")
    ap.add_argument("--out", default="site/data")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--only", default="")
    ap.add_argument("--verify", action="store_true", help="only check feeds, write nothing")
    args = ap.parse_args(argv)

    now = datetime.now(timezone.utc)
    now_iso = now.date().isoformat()
    only = {s.strip().lower() for s in args.only.split(",") if s.strip()} or None
    registry_db = Path(args.registry_db) if args.registry_db else None
    all_approved_companies = load_companies(Path(args.companies), None, registry_db, Path(args.approved_companies),
                                              curated_config=args.curated_config)
    if not all_approved_companies:
        print("No approved employer career sources are ready to crawl. Check companies.yaml and its evidence.", file=sys.stderr)
        return 2
    if only:
        companies = [c for c in all_approved_companies
                     if c.get("slug", "").lower() in only or c.get("ats_slug", "").lower() in only or c["name"].lower() in only]
        if not companies:
            print("No approved company matched --only.", file=sys.stderr)
            return 1
    else:
        companies = all_approved_companies

    http = Http(min_delay=0.4)
    results = []
    if companies:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(crawl_company, c, http, now_iso): c for c in companies}
            for f in as_completed(futs):
                r = f.result()
                results.append(r)
                status = (f"{r['kept']:>4} kept / {r['raw']:>4} raw / "
                          f"{r['rejected']:>3} rejected / {r['review']:>3} review") if r["ok"] else f"FAILED  {r['error']}"
                print(f"{r['company']:<28} {r['ats']:<16} {status}")

    ok = sum(r["ok"] for r in results)
    print(f"\n{ok}/{len(results)} feeds ok, {sum(r['kept'] for r in results)} publishable jobs; "
          f"{sum(r['rejected'] for r in results)} rejected, {sum(r['review'] for r in results)} held for review")
    if args.verify:
        return 0

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    # Fail closed on a total upstream outage: do not replace good published jobs
    # with an empty dataset just because every public board timed out or failed.
    if results and ok == 0:
        print("All configured career feeds failed. Existing public jobs.json was left unchanged.", file=sys.stderr)
        report = {
            "generated_at": now.isoformat(timespec="seconds"),
            "feeds_ok": 0,
            "feeds_total": len(results),
            "jobs_published": 0,
            "jobs_rejected": sum(r["rejected"] for r in results),
            "jobs_held_for_review": sum(r["review"] for r in results),
            "companies": sorted(
                ({k: v for k, v in r.items() if k not in {"jobs", "review_items"}} for r in results),
                key=lambda r: (r["ok"], r["company"]),
            ),
        }
        (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        review_items = [item for result in results for item in result.get("review_items", [])]
        review_path = Path(args.review_out)
        review_path.parent.mkdir(parents=True, exist_ok=True)
        review_path.write_text(json.dumps({"generated_at": now.isoformat(timespec="seconds"), "count": len(review_items),
                                           "items": review_items}, ensure_ascii=False, indent=1), encoding="utf-8")
        return 2
    jobs_path = out / "jobs.json"
    previous = []
    if jobs_path.exists():
        try:
            previous_payload = json.loads(jobs_path.read_text(encoding="utf-8"))
            previous = [] if previous_payload.get("demo") else previous_payload.get("jobs", [])
        except ValueError:
            previous = []
    # Never retain jobs for companies that are no longer approved in the registry.
    approved_slugs = {c.get("slug") for c in all_approved_companies}
    previous = [j for j in previous if j.get("company_slug") in approved_slugs]

    # Partial runs (--only) must not delete everyone else's jobs
    if only:
        crawled = {r["slug"] for r in results}
        carry = [j for j in previous if j.get("company_slug") not in crawled]
        jobs = merge([j for j in previous if j.get("company_slug") in crawled], results, now) + carry
    else:
        # Merge even when every feed fails: recently checked listings may survive
        # a short outage, but MAX_STALE_DAYS still expires them. Never keep them forever.
        jobs = merge(previous, results, now)

    payload = {"generated_at": now.isoformat(timespec="seconds"), "count": len(jobs), "demo": False, "jobs": jobs}
    jobs_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    report = {
        "generated_at": payload["generated_at"],
        "feeds_ok": ok,
        "feeds_total": len(results),
        "jobs_published": sum(r["kept"] for r in results),
        "jobs_rejected": sum(r["rejected"] for r in results),
        "jobs_held_for_review": sum(r["review"] for r in results),
        "companies": sorted(
            ({k: v for k, v in r.items() if k not in {"jobs", "review_items"}} for r in results),
            key=lambda r: (r["ok"], r["company"]),
        ),
    }
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    review_items = [item for result in results for item in result.get("review_items", [])]
    review_path = Path(args.review_out)
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(json.dumps({"generated_at": payload["generated_at"], "count": len(review_items),
                                       "items": review_items}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {jobs_path} ({len(jobs)} jobs); held {len(review_items)} jobs in {review_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
