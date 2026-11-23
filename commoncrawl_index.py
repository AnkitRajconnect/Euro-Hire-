"""Discover public ATS board IDs from Common Crawl's free S3 index files.

The CDX query service can return 504s during bulk discovery. This module uses
the public cluster.idx files on data.commoncrawl.org and HTTP range requests
to read only the index blocks for ATS host prefixes. The range-index method is
adapted from veresk06/open-ats-feed (MIT); see THIRD_PARTY_NOTICES.md.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from ats_pipeline import PoliteHTTP, upsert_board, utc_now

S3_BASE = "https://data.commoncrawl.org/cc-index/collections"
SEED_URL = "https://raw.githubusercontent.com/veresk06/open-ats-feed/main/actor/data/companies.json"
FALLBACK_CRAWLS = [
    "CC-MAIN-2026-04", "CC-MAIN-2025-51", "CC-MAIN-2025-47",
    "CC-MAIN-2025-43", "CC-MAIN-2025-38", "CC-MAIN-2025-33",
    "CC-MAIN-2025-30", "CC-MAIN-2025-26",
]
LEVER_ARCHIVE_CRAWLS = {
    "CC-MAIN-2026-04", "CC-MAIN-2025-51", "CC-MAIN-2025-47",
    "CC-MAIN-2025-43", "CC-MAIN-2025-38", "CC-MAIN-2025-33",
    "CC-MAIN-2025-30", "CC-MAIN-2025-26", "CC-MAIN-2025-21",
    "CC-MAIN-2025-18", "CC-MAIN-2025-13", "CC-MAIN-2025-08",
    "CC-MAIN-2025-05",
}
PROBE_BYTES = 64 * 1024
SCAN_BYTES = 1024 * 1024
RESERVED_TOKENS = {
    "api", "www", "jobs", "careers", "embed", "company", "companies",
    "search", "static", "assets", "cdn", "feed", "login", "signup",
    "robots.txt", "sitemap.xml", "404", "error", "apply", "postings",
}

# A Common Crawl SURT prefix is derived from the ATS host, not a company name.
# Path providers put their tenant slug in the first path segment. Subdomain
# providers (Breezy, Recruitee, Teamtailor and Taleez) put it before the suffix.
HOST_SOURCES = [
    ("greenhouse", "boards.greenhouse.io", "path"),
    ("greenhouse", "job-boards.greenhouse.io", "path"),
    ("greenhouse", "job-boards.eu.greenhouse.io", "path"),
    ("greenhouse", "boards.eu.greenhouse.io", "path"),
    ("lever", "jobs.lever.co", "path"),
    ("lever", "jobs.eu.lever.co", "path"),
    ("ashby", "jobs.ashbyhq.com", "path"),
    ("workable", "apply.workable.com", "path"),
    ("smartrecruiters", "careers.smartrecruiters.com", "path"),
    ("smartrecruiters", "jobs.smartrecruiters.com", "path"),
    ("breezy", "breezy.hr", "subdomain"),
    ("recruitee", "recruitee.com", "subdomain"),
    ("teamtailor", "teamtailor.com", "subdomain"),
    ("taleez", "taleez.com", "subdomain"),
]


def surt_prefix(host: str, token_from: str) -> str:
    reversed_host = ",".join(reversed(host.split(".")))
    return reversed_host + ("," if token_from == "subdomain" else ")/")


def token_from_url(provider: str, host: str, token_from: str, value: str) -> str | None:
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    hostname = (parsed.hostname or "").lower()
    if token_from == "subdomain":
        suffix = "." + host
        if not hostname.endswith(suffix):
            return None
        token = hostname[:-len(suffix)]
        if not token or "." in token:
            return None
    else:
        if hostname != host:
            return None
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            return None
        token = parts[0]
    token = token.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", token):
        return None
    if token.lower() in RESERVED_TOKENS or token.isdigit():
        return None
    return token.lower()


def board_record(provider: str, host: str, token: str, source: str) -> dict:
    eu = provider == "lever" and host == "jobs.eu.lever.co"
    if provider == "greenhouse":
        board_url = f"https://{host}/{token}"
    elif provider == "lever":
        board_url = f"https://{host}/{token}"
    elif provider in {"ashby", "workable"}:
        board_url = f"https://{host}/{token}"
    elif provider == "breezy":
        board_url = f"https://{token}.breezy.hr/"
    elif provider == "recruitee":
        board_url = f"https://{token}.recruitee.com/"
    elif provider == "teamtailor":
        board_url = f"https://{token}.teamtailor.com/jobs"
    elif provider == "taleez":
        board_url = f"https://{token}.taleez.com/careers"
    elif provider == "smartrecruiters":
        board_url = f"https://careers.smartrecruiters.com/{token}"
    else:
        raise ValueError(provider)
    return {
        "ats_type": provider, "ats_slug": token, "board_url": board_url,
        "eu_endpoint": eu, "discovery_source": source,
    }


async def _range(http: PoliteHTTP, url: str, start: int, end: int) -> bytes:
    response = await http.request("GET", url, headers={
        "Range": f"bytes={start}-{end}", "Accept-Encoding": "identity",
    })
    if response.status_code == 200:
        # A server that ignores Range may return the entire ~100MB index.
        raise RuntimeError("Common Crawl storage did not honour HTTP Range")
    if response.status_code != 206:
        raise RuntimeError(f"Expected 206 for byte range, got {response.status_code}")
    return response.content


async def _first_key_at(http: PoliteHTTP, url: str, pos: int, size: int) -> tuple[str, int] | None:
    data = await _range(http, url, pos, min(pos + PROBE_BYTES, size) - 1)
    text = data.decode("utf-8", errors="replace")
    start = 0
    if pos:
        first_newline = text.find("\n")
        if first_newline < 0:
            return None
        start = first_newline + 1
    end = text.find("\n", start)
    if end < 0:
        return None
    return text[start:end].split("\t", 1)[0], pos + start


async def _seek(http: PoliteHTTP, url: str, size: int, prefix: str) -> int:
    low, high = 0, size
    while high - low > PROBE_BYTES:
        middle = (low + high) // 2
        probe = await _first_key_at(http, url, middle, size)
        if probe is None:
            high = middle
        elif probe[0] < prefix:
            low = middle
        else:
            high = middle
    return low


async def _blocks_for(http: PoliteHTTP, url: str, size: int, prefix: str) -> list[dict]:
    blocks: list[dict] = []
    position = await _seek(http, url, size, prefix)
    previous = None
    started = False
    carry = ""
    skip_fragment = position != 0

    while position < size:
        data = await _range(http, url, position, min(position + SCAN_BYTES, size) - 1)
        if not data:
            break
        position += len(data)
        text = carry + data.decode("utf-8", errors="replace")
        last_newline = text.rfind("\n")
        if last_newline < 0:
            carry = text
            continue
        carry = text[last_newline + 1:]
        lines = text[:last_newline].split("\n")
        for line in lines:
            if skip_fragment:
                skip_fragment = False
                continue
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            key = parts[0]
            block = {
                "file": parts[1], "offset": int(parts[2]), "length": int(parts[3]),
            }
            if key < prefix:
                previous = block
                continue
            if key.startswith(prefix):
                if not started:
                    started = True
                    if previous:
                        blocks.append(previous)
                blocks.append(block)
                continue
            if not started and previous:
                blocks.append(previous)
            return blocks
    if not started and previous:
        blocks.append(previous)
    return blocks


async def _read_block(
    http: PoliteHTTP, crawl_id: str, block: dict, prefix: str,
    provider: str, host: str, token_from: str,
) -> set[str]:
    url = f"{S3_BASE}/{crawl_id}/indexes/{block['file']}"
    try:
        compressed = await _range(
            http, url, block["offset"], block["offset"] + block["length"] - 1
        )
        payload = gzip.decompress(compressed).decode("utf-8", errors="replace")
    except Exception as exc:
        print(f"Common Crawl block skipped ({crawl_id}/{block.get('file')}): {exc}")
        return set()
    tokens = set()
    for line in payload.splitlines():
        if not line.startswith(prefix):
            continue
        brace = line.find("{")
        if brace < 0:
            continue
        try:
            record = json.loads(line[brace:])
        except ValueError:
            continue
        token = token_from_url(provider, host, token_from, record.get("url") or "")
        if token:
            tokens.add(token)
    return tokens


async def available_crawls(http: PoliteHTTP, limit: int = 3) -> list[str]:
    recent: list[str] = []
    try:
        payload = await http.get_json("https://index.commoncrawl.org/collinfo.json")
        recent = [
            str(item.get("id")) for item in payload
            if isinstance(item, dict) and str(item.get("id", "")).startswith("CC-MAIN-")
        ][:max(1, limit)]
    except Exception as exc:
        print(f"Common Crawl collection list unavailable; using known snapshots: {exc}")
    # Historical captures are important for Lever: its robots rule was added
    # after these archives were collected, so the old public board URLs remain
    # visible in the archive. The live public API is still queried separately.
    return list(dict.fromkeys(recent + FALLBACK_CRAWLS))


async def seed_from_open_ats_feed(http: PoliteHTTP, conn) -> int:
    """Import verified public board tokens from a public MIT-licensed snapshot.

    Every candidate is probed against its live ATS feed before it can produce
    a French company/job row. The snapshot is only a seed, not a job source.
    """
    try:
        payload = await http.get_json(SEED_URL)
    except Exception as exc:
        print(f"Open ATS seed unavailable; continuing with Common Crawl only: {exc}")
        return 0
    providers = payload.get("providers") if isinstance(payload, dict) else {}
    count = 0
    as_of = payload.get("as_of") if isinstance(payload, dict) else None
    for provider in ("greenhouse", "ashby", "lever", "breezy", "recruitee", "teamtailor"):
        rows = (providers.get(provider) or {}).get("live") or []
        for row in rows:
            if not isinstance(row, list) or not row:
                continue
            slug = str(row[0])
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", slug):
                continue
            if provider == "greenhouse":
                host = "boards.greenhouse.io"
            elif provider == "lever":
                host = "jobs.lever.co"
            elif provider == "ashby":
                host = "jobs.ashbyhq.com"
            elif provider == "breezy":
                host = "breezy.hr"
            elif provider == "recruitee":
                host = "recruitee.com"
            else:
                host = "teamtailor.com"
            source = board_record(provider, host, slug, f"open-ats-feed MIT snapshot {as_of}")
            source["seed_posting_count"] = int(row[1]) if len(row) > 1 and str(row[1]).isdigit() else None
            upsert_board(conn, source)
            count += 1
    conn.commit()
    print(f"Imported {count} ATS board candidates from open-ats-feed snapshot ({as_of})")
    return count


async def harvest_commoncrawl(http: PoliteHTTP, conn, crawl_limit: int = 3) -> int:
    crawls = await available_crawls(http, crawl_limit)
    found_total = 0
    for crawl_id in crawls:
        index_url = f"{S3_BASE}/{crawl_id}/indexes/cluster.idx"
        try:
            head = await http.request("HEAD", index_url)
            size = int(head.headers.get("content-length") or 0)
            if size <= 0:
                print(f"No cluster.idx size for {crawl_id}; skipping")
                continue
        except Exception as exc:
            print(f"Could not read cluster.idx for {crawl_id}: {exc}")
            continue

        for provider, host, token_from in HOST_SOURCES:
            if provider == "lever" and crawl_id not in LEVER_ARCHIVE_CRAWLS:
                continue
            done = conn.execute(
                "SELECT 1 FROM commoncrawl_index_runs WHERE crawl_id=? AND host=?",
                (crawl_id, host),
            ).fetchone()
            if done:
                continue
            prefix = surt_prefix(host, token_from)
            try:
                blocks = await _blocks_for(http, index_url, size, prefix)
                tokens: set[str] = set()
                for block in blocks:
                    tokens.update(await _read_block(
                        http, crawl_id, block, prefix, provider, host, token_from
                    ))
                for token in tokens:
                    board = board_record(
                        provider, host, token, f"Common Crawl S3 {crawl_id}"
                    )
                    upsert_board(conn, board)
                conn.execute("""
                  INSERT OR REPLACE INTO commoncrawl_index_runs
                    (crawl_id, host, provider, harvested_at, tokens_found)
                  VALUES (?, ?, ?, ?, ?)
                """, (crawl_id, host, provider, utc_now(), len(tokens)))
                conn.commit()
                found_total += len(tokens)
                print(f"{crawl_id} {host}: {len(blocks)} index blocks, {len(tokens)} board IDs")
            except Exception as exc:
                print(f"Common Crawl source {crawl_id} {host} failed: {exc}")
                # Do not mark a failed index/host as harvested; retry next week.
    return found_total
