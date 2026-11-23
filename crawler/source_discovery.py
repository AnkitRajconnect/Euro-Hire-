"""Discover public ATS board URLs via the free Common Crawl URL index."""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse

from .http import FetchError, Http

# These patterns name ATS providers, not employers. A result is accepted only
# if its URL matches the board-root shape for that provider.
SEARCHES = [
    ("greenhouse", "boards.greenhouse.io/*", r"^https?://boards\.greenhouse\.io/[^/?#]+/?$"),
    ("greenhouse", "job-boards.greenhouse.io/*", r"^https?://job-boards\.greenhouse\.io/[^/?#]+/?$"),
    ("greenhouse", "job-boards.eu.greenhouse.io/*", r"^https?://job-boards\.eu\.greenhouse\.io/[^/?#]+/?$"),
    ("lever", "jobs.lever.co/*", r"^https?://jobs\.lever\.co/[^/?#]+/?$"),
    ("lever", "jobs.eu.lever.co/*", r"^https?://jobs\.eu\.lever\.co/[^/?#]+/?$"),
    ("ashby", "jobs.ashbyhq.com/*", r"^https?://jobs\.ashbyhq\.com/[^/?#]+/?$"),
    ("workable", "apply.workable.com/*", r"^https?://apply\.workable\.com/[^/?#]+/?$"),
    ("smartrecruiters", "jobs.smartrecruiters.com/*", r"^https?://jobs\.smartrecruiters\.com/[^/?#]+/?$"),
    ("personio", "*.jobs.personio.de", r"^https?://[a-z0-9-]+\.jobs\.personio\.de/?$"),
    ("personio", "*.jobs.personio.com", r"^https?://[a-z0-9-]+\.jobs\.personio\.com/?$"),
    ("recruitee", "*.recruitee.com", r"^https?://[a-z0-9-]+\.recruitee\.com/?$"),
    ("teamtailor", "*.teamtailor.com", r"^https?://[a-z0-9-]+\.teamtailor\.com/jobs/?$"),
    ("workday", "*.myworkdayjobs.com", r"^https?://[a-z0-9-]+\.wd[0-9]+\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?[A-Za-z0-9_-]+/?$"),
    ("breezy", "*.breezy.hr", r"^https?://[a-z0-9-]+\.breezy\.hr/?$"),
    ("pinpoint", "*.pinpointhq.com", r"^https?://[a-z0-9-]+\.pinpointhq\.com/?$"),
    ("bamboohr", "*.bamboohr.com", r"^https?://[a-z0-9-]+\.bamboohr\.com/careers/list/?$"),
    ("jazzhr", "*.applytojob.com", r"^https?://[a-z0-9-]+\.applytojob\.com/apply/?$"),
    ("keka", "*.keka.com", r"^https?://[a-z0-9-]+\.keka\.com/careers/?$"),
    ("homerun", "feed.homerun.co/*", r"^https?://feed\.homerun\.co/[a-z0-9-]+/?$"),
    ("gem", "jobs.gem.com/*", r"^https?://jobs\.gem\.com/[a-z0-9-]+/?$"),
    ("rippling", "ats.rippling.com/*", r"^https?://ats\.rippling\.com/[a-z0-9-]+/jobs/?$"),
]

RESERVED = {"api", "www", "jobs", "careers", "embed", "company", "companies",
            "search", "static", "assets", "cdn", "feed", "login", "signup"}


def _record(ats, slug, board_url, **extra):
    slug = slug.strip()
    if not slug or slug.lower() in RESERVED:
        return None
    return {
        "ats": ats, "slug": slug, "ats_slug": slug,
        "company_key": ats + ":" + slug,
        "name": slug.replace("-", " ").replace("_", " ").title(),
        "board_url": board_url, **extra,
    }


def extract_board(ats: str, value: str) -> dict | None:
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    parts = [part for part in parsed.path.split("/") if part]

    if ats == "greenhouse" and host in {"boards.greenhouse.io", "job-boards.greenhouse.io", "job-boards.eu.greenhouse.io"} and parts:
        return _record(ats, parts[0], f"https://{host}/{parts[0]}", eu=("eu." in host))
    if ats == "lever" and host in {"jobs.lever.co", "jobs.eu.lever.co"} and parts:
        return _record(ats, parts[0], f"https://{host}/{parts[0]}", eu=(host == "jobs.eu.lever.co"))
    if ats == "ashby" and host == "jobs.ashbyhq.com" and parts:
        return _record(ats, parts[0], f"https://{host}/{parts[0]}")
    if ats == "workable" and host == "apply.workable.com" and parts:
        return _record(ats, parts[0], f"https://{host}/{parts[0]}")
    if ats == "smartrecruiters" and host == "jobs.smartrecruiters.com" and parts:
        return _record(ats, parts[0], f"https://{host}/{parts[0]}")
    match = re.fullmatch(r"([a-z0-9-]+)\.jobs\.personio\.(de|com)", host)
    if ats == "personio" and match:
        return _record(ats, match.group(1), f"https://{host}/xml?language=en")
    match = re.fullmatch(r"([a-z0-9-]+)\.recruitee\.com", host)
    if ats == "recruitee" and match:
        return _record(ats, match.group(1), f"https://{host}/api/offers/")
    match = re.fullmatch(r"([a-z0-9-]+)\.teamtailor\.com", host)
    if ats == "teamtailor" and match:
        return _record(ats, match.group(1), f"https://{host}/jobs.rss")
    match = re.fullmatch(r"([a-z0-9-]+)\.(wd[0-9]+)\.myworkdayjobs\.com", host)
    if ats == "workday" and match and parts:
        if re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[0]):
            locale, site = parts[0], parts[1] if len(parts) > 1 else ""
        else:
            locale, site = "en-US", parts[0]
        if not site:
            return None
        slug = "|".join((match.group(1), match.group(2), site, locale))
        return {
            "ats": ats, "slug": slug, "ats_slug": slug, "company_key": ats + ":" + slug,
            "name": match.group(1).replace("-", " ").title(),
            "board_url": f"https://{host}/{locale}/{site}",
        }
    for provider, suffix, path in (
        ("breezy", ".breezy.hr", "/"),
        ("pinpoint", ".pinpointhq.com", "/postings.json"),
        ("bamboohr", ".bamboohr.com", "/careers/list"),
        ("jazzhr", ".applytojob.com", "/apply"),
        ("keka", ".keka.com", "/careers"),
    ):
        if ats == provider and host.endswith(suffix):
            tenant = host[:-len(suffix)]
            if re.fullmatch(r"[a-z0-9-]+", tenant):
                return _record(ats, tenant, f"https://{host}{path}")
    if ats == "homerun" and host == "feed.homerun.co" and parts:
        return _record(ats, parts[0], f"https://{host}/{parts[0]}")
    if ats == "gem" and host == "jobs.gem.com" and parts:
        return _record(ats, parts[0], f"https://{host}/{parts[0]}")
    if ats == "rippling" and host == "ats.rippling.com" and len(parts) >= 2 and parts[1] == "jobs":
        return _record(ats, parts[0], f"https://{host}/{parts[0]}/jobs")
    return None


def _latest_collection(http: Http) -> dict:
    collections = http.get_json("https://index.commoncrawl.org/collinfo.json")
    for collection in collections:
        if str(collection.get("id", "")).startswith("CC-MAIN-"):
            return collection
    raise FetchError("Common Crawl did not return a current CC-MAIN collection")


def refresh_index(out_path: Path, max_per_pattern: int = 10000,
                  refresh_days: int = 7, force: bool = False) -> dict:
    # Common Crawl asks clients to sleep between index calls and not parallelise.
    http = Http(min_delay=2.0, timeout=25, retries=1)
    latest = _latest_collection(http)
    collection_id = latest["id"]

    previous = {}
    if out_path.exists():
        try:
            previous = json.loads(out_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = {}
    previous_boards = {
        f"{item.get('ats')}:{item.get('ats_slug') or item.get('slug')}": item
        for item in previous.get("boards", []) if isinstance(item, dict)
    }
    try:
        updated = datetime.fromisoformat(str(previous.get("updated_at", "")).replace("Z", "+00:00"))
    except ValueError:
        updated = datetime.min.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if (not force and previous.get("collection") == collection_id
            and now - updated < timedelta(days=max(1, refresh_days)) and previous_boards):
        print(f"Using cached Common Crawl board index: {len(previous_boards)} public boards")
        return previous

    index_url = latest.get("cdx-api") or f"https://index.commoncrawl.org/{collection_id}-index"
    discovered = dict(previous_boards)
    query_results = []
    successes = 0
    for ats, pattern, url_regex in SEARCHES:
        try:
            body = http.get_text(index_url, params=[
                ("url", pattern), ("output", "json"), ("filter", "status:200"),
                ("filter", f"~url:{url_regex}"), ("collapse", "urlkey"),
                ("limit", str(max(1, min(max_per_pattern, 10000)))),
            ])
            found = 0
            for line in body.splitlines():
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                url = record.get("url") or record.get("original") or ""
                board = extract_board(ats, url)
                if not board:
                    continue
                key = f"{board['ats']}:{board['ats_slug']}"
                old = discovered.get(key, {})
                board["first_seen"] = old.get("first_seen") or now.date().isoformat()
                board["last_seen_in_index"] = now.date().isoformat()
                board["last_crawled"] = old.get("last_crawled")
                board["last_successful"] = old.get("last_successful")
                discovered[key] = board
                found += 1
            successes += 1
            query_results.append({"ats": ats, "pattern": pattern, "ok": True, "boards_found": found})
        except (FetchError, ValueError, OSError) as error:
            query_results.append({"ats": ats, "pattern": pattern, "ok": False,
                                  "boards_found": 0, "error": str(error)[:240]})
            print(f"! Common Crawl query failed for {ats}: {error}", file=sys.stderr)

    if successes == 0 and not previous_boards:
        raise FetchError("All ATS URL-index queries failed and no previous board index exists")
    payload = {
        "version": 1,
        "source": "Common Crawl public URL index and live public employer ATS feeds",
        "collection": collection_id,
        "collection_from": latest.get("from"),
        "collection_to": latest.get("to"),
        "updated_at": now.isoformat(timespec="seconds"),
        "boards_count": len(discovered),
        "ats_supported": sorted({item["ats"] for item in discovered.values()}),
        "query_results": query_results,
        "boards": sorted(discovered.values(), key=lambda item: (item["ats"], item["ats_slug"])),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Discovered or retained {len(discovered)} public ATS boards from {successes}/{len(SEARCHES)} URL-index queries")
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="site/data/boards.json")
    parser.add_argument("--max-per-pattern", type=int, default=10000)
    parser.add_argument("--refresh-days", type=int, default=7)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        refresh_index(Path(args.out), args.max_per_pattern, args.refresh_days, args.force)
    except (FetchError, OSError, ValueError) as error:
        print(f"ATS board discovery failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
