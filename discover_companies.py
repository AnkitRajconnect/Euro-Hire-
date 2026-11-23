#!/usr/bin/env python3
"""Weekly discovery of live public ATS boards with jobs located in France."""
from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import re
import sys
from pathlib import Path
from typing import Any

import httpx

from job_pipeline import (
    COMMONCRAWL_BASE, COMMONCRAWL_META_URL, PROVIDERS, SEED_DATA_URL,
    HostRateLimiter, create_database, fetch_board_jobs, norm_text,
    slug_to_name, upsert_company, upsert_source, utc_now,
)

PROBE_BYTES = 64 * 1024
SCAN_BYTES = 1024 * 1024
SUFFIXES = {
    "the", "inc", "incorporated", "ltd", "limited", "llc", "llp", "plc",
    "sas", "sarl", "sa", "sasu", "gmbh", "bv", "nv", "ab", "oy", "as",
    "france", "group", "holding", "holdings", "company", "co",
}


def comparable_name(value: Any) -> str:
    words = norm_text(value).split()
    while words and words[-1] in SUFFIXES:
        words.pop()
    return " ".join(words)


async def _cc_request(client, url, headers=None, retries=3):
    for attempt in range(retries):
        try:
            response = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            if attempt + 1 == retries:
                return None, str(exc)
            await asyncio.sleep(2 ** attempt)
            continue
        if response.status_code in (429, 500, 502, 503, 504):
            if attempt + 1 == retries:
                return response, f"HTTP {response.status_code}"
            await asyncio.sleep(min(2 ** (attempt + 1), 30))
            continue
        if response.status_code >= 400:
            return response, f"HTTP {response.status_code}"
        return response, None
    return None, "request failed"


async def ranged_get(client, url, start, end):
    response, error = await _cc_request(
        client, url, headers={"Range": f"bytes={start}-{end}"}, retries=4
    )
    if error or response is None:
        raise RuntimeError(error or "Common Crawl range request failed")
    if response.status_code == 206:
        return response.content
    if response.status_code == 200 and start == 0 and len(response.content) <= SCAN_BYTES:
        return response.content
    raise RuntimeError(f"Common Crawl server ignored byte range {start}-{end}")


async def first_key_at(client, url, position, size):
    body = await ranged_get(client, url, position, min(position + PROBE_BYTES, size) - 1)
    text = body.decode("utf-8", errors="replace")
    start = 0
    if position:
        newline = text.find("\n")
        if newline < 0:
            return None
        start = newline + 1
    end = text.find("\n", start)
    if end < 0:
        return None
    line = text[start:end]
    return line.split("\t", 1)[0], position + start


async def seek_prefix(client, url, size, prefix):
    low, high = 0, size
    while high - low > PROBE_BYTES:
        middle = (low + high) // 2
        probe = await first_key_at(client, url, middle, size)
        if probe is None:
            high = middle
            continue
        if probe[0] < prefix:
            low = middle
        else:
            high = middle
    return low


async def blocks_for_prefix(client, url, size, prefix):
    blocks, position = [], await seek_prefix(client, url, size, prefix)
    previous, started, carry = None, False, ""
    at_line_start = position == 0
    while position < size:
        body = await ranged_get(client, url, position, min(position + SCAN_BYTES, size) - 1)
        if not body:
            break
        text = carry + body.decode("utf-8", errors="replace")
        position += len(body)
        last_newline = text.rfind("\n")
        if last_newline < 0:
            carry = text
            continue
        carry, text = text[last_newline + 1:], text[:last_newline]
        for line in text.split("\n"):
            if not at_line_start:
                at_line_start = True
                continue
            if not line:
                continue
            fields = line.split("\t")
            if len(fields) < 4:
                continue
            try:
                block = {"file": fields[1], "offset": int(fields[2]), "length": int(fields[3])}
            except ValueError:
                continue
            key = fields[0]
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


async def read_index_block(client, crawl_id, block, prefix, provider, tokens):
    url = f"{COMMONCRAWL_BASE}/{crawl_id}/indexes/{block['file']}"
    body = await ranged_get(client, url, block["offset"], block["offset"] + block["length"] - 1)
    try:
        text = gzip.decompress(body).decode("utf-8", errors="replace")
    except (OSError, EOFError) as exc:
        raise RuntimeError(f"invalid gzip index block: {exc}") from exc
    added = 0
    for line in text.splitlines():
        if not line.startswith(prefix):
            continue
        brace = line.find("{")
        if brace < 0:
            continue
        try:
            record = json.loads(line[brace:])
        except ValueError:
            continue
        token = board_token(provider, record.get("url", ""))
        if token and token not in tokens:
            tokens.add(token)
            added += 1
    return added


def board_token(provider: str, url: str) -> str | None:
    from job_pipeline import extract_board_token
    return extract_board_token(provider, url)


async def harvest_common_crawl(client, tokens, max_crawls):
    response, error = await _cc_request(client, COMMONCRAWL_META_URL, retries=2)
    if error or response is None:
        raise RuntimeError(f"collection metadata failed: {error}")
    collections = response.json()
    crawl_ids = [
        str(item["id"]) for item in collections
        if str(item.get("id", "")).startswith("CC-MAIN-")
    ][:max(1, max_crawls)]
    additions = {provider: 0 for provider in PROVIDERS}
    for crawl_id in crawl_ids:
        index_url = f"{COMMONCRAWL_BASE}/{crawl_id}/indexes/cluster.idx"
        head = await client.head(index_url)
        if head.status_code >= 400:
            print(f"! {crawl_id}: cluster.idx HEAD HTTP {head.status_code}", file=sys.stderr)
            continue
        size = int(head.headers.get("content-length", "0"))
        if not size:
            continue
        print(f"Harvesting {crawl_id} S3 index")
        for provider, config in PROVIDERS.items():
            for host, mode in config["hosts"]:
                reverse = ",".join(reversed(host.split(".")))
                prefix = reverse + ("," if mode in {"subdomain", "workday"} else ")/")
                try:
                    blocks = await blocks_for_prefix(client, index_url, size, prefix)
                    added = 0
                    for block in blocks:
                        added += await read_index_block(
                            client, crawl_id, block, prefix, provider, tokens[provider]
                        )
                    additions[provider] += added
                    if added:
                        print(f"  {provider} / {host}: +{added} board tokens")
                except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                    print(f"  ! {provider} / {host}: {exc}", file=sys.stderr)
    return additions


async def load_seed_tokens(client):
    tokens = {provider: set() for provider in PROVIDERS}
    response, error = await _cc_request(client, SEED_DATA_URL, retries=3)
    if error or response is None:
        print(f"! Optional public ATS seed snapshot unavailable: {error}", file=sys.stderr)
        return tokens, None
    payload = response.json()
    for provider in set(PROVIDERS) & set(payload.get("providers", {})):
        for entry in payload["providers"][provider].get("live", []):
            value = entry[0] if isinstance(entry, (list, tuple)) and entry else entry
            if value:
                tokens[provider].add(str(value).strip().lower())
    return tokens, payload.get("as_of")


async def registry_lookup(client, limiter, display_name):
    query = comparable_name(display_name)
    if len(query) < 3:
        return None, "unmatched"
    response, error = await _request_registry(client, limiter, display_name)
    if error or response is None:
        return None, "api_error"
    try:
        results = response.json().get("results", [])
    except (ValueError, AttributeError):
        return None, "api_error"
    exact = []
    for item in results:
        if item.get("etat_administratif") != "A":
            continue
        if str(item.get("nature_juridique") or "") == "1000":
            continue
        if item.get("statut_diffusion") != "O":
            continue
        siege = item.get("siege") or {}
        aliases = (
            item.get("nom_complet"), item.get("nom_raison_sociale"),
            item.get("sigle"), siege.get("nom_commercial"),
        )
        if any(query == comparable_name(alias) for alias in aliases if alias):
            exact.append(item)
    if len(exact) == 1:
        return exact[0], "exact"
    if len(exact) > 1:
        return None, "ambiguous"
    return None, "unmatched"


async def _request_registry(client, limiter, display_name):
    from job_pipeline import request_with_retries
    return await request_with_retries(
        client,
        "https://recherche-entreprises.api.gouv.fr/search",
        limiter=limiter,
        interval=0.20,
        params={
            "q": display_name, "etat_administratif": "A",
            "per_page": "10", "page": "1", "minimal": "true", "include": "siege",
        },
        headers={"Accept": "application/json"},
        retries=3,
    )


async def run_discovery(db_path, report_path, max_crawls=2, seed_only=False, max_candidates=0):
    headers = {"User-Agent": "EuroHireDiscovery/1.0 (+https://github.com/AnkitRajconnect/Euro-Hire-)"}
    timeout = httpx.Timeout(35.0, connect=15.0)
    limits = httpx.Limits(max_connections=40, max_keepalive_connections=20)
    connection = create_database(db_path)
    checked_at = utc_now()
    async with httpx.AsyncClient(timeout=timeout, limits=limits, follow_redirects=True, headers=headers) as client:
        tokens, seed_as_of = await load_seed_tokens(client)
        harvested = {provider: 0 for provider in PROVIDERS}
        if not seed_only:
            try:
                harvested = await harvest_common_crawl(client, tokens, max_crawls)
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                print(f"! Common Crawl S3 refresh failed; retaining seed tokens: {exc}", file=sys.stderr)

        candidates = [(provider, slug) for provider, slugs in tokens.items() for slug in sorted(slugs)]
        if max_candidates > 0:
            candidates = candidates[:max_candidates]
        report = {
            "generated_at": checked_at,
            "seed_source": SEED_DATA_URL,
            "seed_snapshot_as_of": seed_as_of,
            "seed_license": "MIT",
            "common_crawl_method": "S3 cluster.idx byte-range reads; no CDX bulk query",
            "candidate_boards": len(candidates),
            "candidates_by_provider": {key: len(value) for key, value in tokens.items()},
            "new_candidates_from_common_crawl": harvested,
            "live_boards_checked": 0,
            "boards_with_france_jobs": 0,
            "jobs_in_france": 0,
            "registry_matches": 0,
            "registry_unmatched": 0,
            "registry_api_errors": 0,
            "provider_results": {},
            "errors": [],
        }
        semaphores = {
            provider: asyncio.Semaphore(int(config["concurrency"]))
            for provider, config in PROVIDERS.items()
        }
        board_limiter = HostRateLimiter()
        found = []

        async def probe(provider, slug):
            async with semaphores[provider]:
                success, jobs, error = await fetch_board_jobs(provider, slug, client, board_limiter)
            if success:
                report["live_boards_checked"] += 1
                if jobs:
                    found.append((provider, slug, jobs))
                    report["boards_with_france_jobs"] += 1
                    report["jobs_in_france"] += len(jobs)
            elif error and "HTTP 404" not in error and "HTTP 410" not in error:
                report["errors"].append({"provider": provider, "slug": slug, "error": error[:200]})

        await asyncio.gather(*(probe(provider, slug) for provider, slug in candidates))

        registry_limiter = HostRateLimiter()
        for provider, slug, jobs in found:
            display_name = next((job.get("company_name") for job in jobs if job.get("company_name")), "") or slug_to_name(slug.split("|", 1)[0] if provider == "workday" else slug)
            registry, match_status = await registry_lookup(client, registry_limiter, display_name)
            report["registry_matches"] += int(match_status == "exact")
            report["registry_api_errors"] += int(match_status == "api_error")
            report["registry_unmatched"] += int(match_status not in {"exact", "api_error"})
            company_id = upsert_company(
                connection, f"{provider}:{slug}", display_name,
                registry, match_status, checked_at
            )
            upsert_source(connection, company_id, display_name, provider, slug, checked_at)
            stats = report["provider_results"].setdefault(provider, {"boards": 0, "france_jobs": 0})
            stats["boards"] += 1
            stats["france_jobs"] += len(jobs)
        connection.commit()

    connection.close()
    report_path = Path(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"Discovered {report['boards_with_france_jobs']} boards and "
        f"{report['jobs_in_france']} open France jobs; "
        f"{report['registry_matches']} exact SIREN matches."
    )
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Discover active public ATS boards with jobs in France.")
    parser.add_argument("--db", default="data/jobs.db")
    parser.add_argument("--report", default="data/discovery_report.json")
    parser.add_argument("--max-crawls", type=int, default=2)
    parser.add_argument("--max-candidates", type=int, default=0)
    parser.add_argument("--seed-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(run_discovery(
            args.db, args.report, args.max_crawls, args.seed_only, args.max_candidates
        ))
    except Exception as exc:
        print(f"Discovery failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0 if report["boards_with_france_jobs"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
