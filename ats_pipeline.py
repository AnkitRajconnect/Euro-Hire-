"""France-first ATS ingestion, public WTTJ feed and INSEE enrichment.

Only public vacancy endpoints are used. No applicant data, private recruiter
contact data, paid job APIs, cookies or authenticated employer APIs are collected.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import html
import json
import os
import re
import sqlite3
import time
import unicodedata
import urllib.parse
import urllib.robotparser
from collections import defaultdict
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import feedparser
import httpx

ROOT = Path(__file__).resolve().parent
DB_DEFAULT = ROOT / "data" / "jobs.db"
JSON_DEFAULT = ROOT / "data" / "active_jobs.json"
CSV_DEFAULT = ROOT / "data" / "active_jobs.csv"
SITE_JSON_DEFAULT = ROOT / "site" / "data" / "jobs.json"
REPORT_DEFAULT = ROOT / "data" / "pipeline_report.json"

USER_AGENT = (
    "EuroHireFrance/1.0 (+https://github.com/AnkitRajconnect/Euro-Hire-; "
    "public job listing index; no applicant data)"
)
INSEE_URL = "https://recherche-entreprises.api.gouv.fr/search"
WTTJ_ORIGIN = "https://www.welcometothejungle.com"
WTTJ_ENV_URL = WTTJ_ORIGIN + "/api/env"
WTTJ_INDEX = "wttj_jobs_production_en"
WTTJ_ATTRS = (
    "name,slug,organization,offices,remote,published_at_timestamp,"
    "salary_yearly_minimum,salary_maximum,salary_period,salary_currency,"
    "contract_type,department,new_profession,experience_level_minimum"
)
WTTJ_SEED_SOURCE = (
    "https://raw.githubusercontent.com/veresk06/open-ats-feed/main/actor/data/companies.json"
)

# Common Crawl providers that expose unauthenticated public vacancy feeds.
# SmartRecruiters is intentionally absent: its API host disallows general
# crawlers in robots.txt. Personio's historical tenant robots rules disallow /xml.
ATS_PROVIDERS = {
    "greenhouse", "lever", "ashby", "workable",
    "breezy", "recruitee", "teamtailor", "taleez", "smartrecruiters",
}
PROVIDER_CONCURRENCY = {
    "greenhouse": 5, "lever": 1, "ashby": 5, "workable": 1,
    "breezy": 5, "recruitee": 5, "teamtailor": 5, "taleez": 4,
}
HOST_DELAY = {
    "api.lever.co": 1.0,
    "api.eu.lever.co": 1.0,
    "apply.workable.com": 1.0,
    "index.commoncrawl.org": 2.0,
    "recherche-entreprises.api.gouv.fr": 0.22,
    "welcometothejungle.com": 0.5,
}
RECRUITEE_TOKEN_DATE = date(2027, 2, 10)

FR_CITIES = {
    "paris": (48.8566, 2.3522), "lyon": (45.7640, 4.8357),
    "marseille": (43.2965, 5.3698), "toulouse": (43.6047, 1.4442),
    "nice": (43.7102, 7.2620), "nantes": (47.2184, -1.5536),
    "montpellier": (43.6108, 3.8767), "strasbourg": (48.5734, 7.7521),
    "bordeaux": (44.8378, -0.5792), "lille": (50.6292, 3.0573),
    "rennes": (48.1173, -1.6778), "reims": (49.2583, 4.0317),
    "le havre": (49.4944, 0.1079), "saint-etienne": (45.4397, 4.3872),
    "toulon": (43.1242, 5.9280), "grenoble": (45.1885, 5.7245),
    "dijon": (47.3220, 5.0415), "angers": (47.4784, -0.5632),
    "nimes": (43.8367, 4.3601), "villeurbanne": (45.7667, 4.8803),
    "clermont-ferrand": (45.7772, 3.0870), "le mans": (48.0061, 0.1996),
    "aix-en-provence": (43.5297, 5.4474), "brest": (48.3904, -4.4861),
    "tours": (47.3941, 0.6848), "amiens": (49.8941, 2.2958),
    "limoges": (45.8336, 1.2611), "annecy": (45.8992, 6.1294),
    "perpignan": (42.6887, 2.8948), "metz": (49.1193, 6.1757),
    "besancon": (47.2378, 6.0241), "orleans": (47.9029, 1.9093),
    "mulhouse": (47.7508, 7.3359), "rouen": (49.4432, 1.0999),
    "caen": (49.1829, -0.3707), "nancy": (48.6921, 6.1844),
    "poitiers": (46.5802, 0.3404), "pau": (43.2951, -0.3708),
    "la rochelle": (46.1603, -1.1511), "lorient": (47.7483, -3.3702),
    "valence": (44.9334, 4.8924), "antibes": (43.5808, 7.1239),
}
FR_REGIONS = (
    "ile de france", "auvergne rhone alpes", "nouvelle aquitaine",
    "occitanie", "hauts de france", "provence alpes cote d azur",
    "bretagne", "normandie", "grand est", "pays de la loire",
    "bourgogne franche comte", "centre val de loire", "corse",
)

ROLE_RULES = [
    ("engineering", r"engineer|developer|software|devops|sre|backend|frontend|full.?stack|security|platform|architect|qa\b|mobile|ios|android|ingénieur|développeur"),
    ("data", r"data scientist|data engineer|data analyst|machine learning|\bml\b|\bai\b|analytics|bi analyst|research scientist"),
    ("product", r"product manager|product owner|product lead|chef de produit"),
    ("design", r"design|ux|ui\b|creative|brand|graphiste"),
    ("sales", r"sales|account executive|account manager|business development|\bbdr\b|\bsdr\b|partnership|commercial"),
    ("marketing", r"marketing|growth|content|seo|communication|social media|public relations"),
    ("support", r"customer success|customer support|customer service|onboarding|client success"),
    ("finance", r"finance|accountant|accounting|controller|tax|audit|risk|treasury|comptable"),
    ("legal", r"legal|lawyer|juriste|compliance|privacy officer|data protection"),
    ("people", r"recruit|talent|people|\bhr\b|human resources|ressources humaines"),
    ("procurement", r"procurement|purchasing|buyer|achats"),
    ("admin", r"administrative|administrator|office manager|executive assistant|assistant administratif"),
    ("business", r"business analyst|management consultant|strategy consultant|business operations|consultant"),
    ("operations", r"operations|supply chain|logistics|project manager|program manager|chef de projet"),
    ("healthcare", r"physician|doctor|nurse|pharmacist|dentist|clinical|medical|médecin|infirmier|pharmacien"),
    ("education", r"teacher|lecturer|professor|academic|education|enseignant|professeur"),
]
LEVEL_RULES = [
    ("intern", r"\bintern(ship)?\b|stagiaire|stage\b|alternance|apprentice|working student"),
    ("junior", r"\bjunior\b|\bgraduate\b|entry.?level|\bassociate\b|trainee"),
    ("lead", r"\b(lead|staff|principal|head of|director|vp|chief)\b|\bmanager\b"),
    ("senior", r"\bsenior\b|\bsr\b|\bexperienced\b"),
]
LEGAL_SUFFIXES = {
    "sas", "sasu", "sarl", "sa", "eurl", "scop", "gie", "sci", "sasu",
    "societe", "société", "company", "limited", "ltd", "inc", "corp",
    "corporation", "gmbh", "ag", "bv", "nv", "plc", "llc",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def norm_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = html.unescape(text).lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def company_name_from_slug(slug: str) -> str:
    return re.sub(r"[-_.]+", " ", str(slug or "")).strip().title()


def is_france_location(location: str, country: str | None = None) -> tuple[bool, str | None]:
    """Return whether a role is demonstrably France-based and its best city.

    A generic 'Remote' role is not treated as France-based merely because the
    employer is French. Remote roles must explicitly name France or a French
    office/country in the source payload.
    """
    code = str(country or "").strip().upper()
    raw = str(location or "").strip()
    text = norm_text(raw)
    if code in {"FR", "FRANCE"}:
        city = next((name.title() for name in FR_CITIES if re.search(rf"\b{re.escape(name)}\b", text)), None)
        return True, city
    if code and code not in {"FR", "FRANCE"}:
        return False, None
    if not text:
        return False, None
    if re.search(r"\b(remote|worldwide|europe|emea|global)\b", text) and "france" not in text and "fr " not in text:
        # Keep explicit "Remote France"; drop Remote Europe/global.
        if not re.search(r"\b(remote france|france remote|fr remote|remote fr)\b", text):
            return False, None
    city = next((name.title() for name in FR_CITIES if re.search(rf"\b{re.escape(name)}\b", text)), None)
    if re.search(r"\b(france|francaise|francais|fr)\b", text) or city:
        return True, city
    if any(region in text for region in FR_REGIONS):
        return True, None
    return False, None


def coordinates(city: str | None) -> tuple[float, float]:
    if city:
        key = norm_text(city)
        for name, coords in FR_CITIES.items():
            if norm_text(name) == key:
                return coords
    return (46.6034, 1.8883)  # France centroid; city not published by source.


def classify_role(title: str) -> str:
    text = str(title or "").lower()
    for role, pattern in ROLE_RULES:
        if re.search(pattern, text, re.I):
            return role
    return "other"


def classify_level(title: str) -> str:
    for level, pattern in LEVEL_RULES:
        if re.search(pattern, str(title or ""), re.I):
            return level
    return "mid"


def parse_date(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        stamp = float(value)
        if stamp > 10_000_000_000:
            stamp /= 1000
        try:
            return datetime.fromtimestamp(stamp, timezone.utc).date().isoformat()
        except (ValueError, OSError, OverflowError):
            return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").date().isoformat()
        except ValueError:
            return None


def strip_html(value: Any) -> str:
    raw = html.unescape(str(value or ""))
    raw = re.sub(r"(?is)<(script|style)\b.*?>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6])>", " ", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS ats_boards (
      board_key TEXT PRIMARY KEY,
      ats_type TEXT NOT NULL,
      ats_slug TEXT NOT NULL,
      board_url TEXT NOT NULL,
      eu_endpoint INTEGER NOT NULL DEFAULT 0,
      discovery_source TEXT NOT NULL,
      seed_posting_count INTEGER,
      first_seen_at TEXT NOT NULL,
      last_seen_at TEXT NOT NULL,
      last_probed_at TEXT,
      last_success_at TEXT,
      last_error TEXT,
      has_fr_jobs INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_ats_boards_queue
      ON ats_boards(has_fr_jobs, last_probed_at, ats_type);
    CREATE TABLE IF NOT EXISTS companies (
      company_key TEXT PRIMARY KEY,
      name TEXT NOT NULL,
      website TEXT,
      ats_type TEXT NOT NULL,
      ats_slug TEXT NOT NULL,
      siren TEXT,
      legal_name TEXT,
      naf_code TEXT,
      naf_code_rev2 TEXT,
      naf_label TEXT,
      legal_city TEXT,
      legal_form TEXT,
      insee_match_status TEXT NOT NULL DEFAULT 'pending',
      insee_match_score REAL,
      insee_checked_at TEXT,
      first_seen_at TEXT NOT NULL,
      last_seen_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_companies_naf ON companies(naf_code);
    CREATE INDEX IF NOT EXISTS idx_companies_siren ON companies(siren);
    CREATE TABLE IF NOT EXISTS jobs (
      job_key TEXT PRIMARY KEY,
      dedupe_key TEXT NOT NULL,
      company_key TEXT NOT NULL REFERENCES companies(company_key),
      source_type TEXT NOT NULL,
      source_id TEXT NOT NULL,
      title TEXT NOT NULL,
      department TEXT,
      location TEXT,
      city TEXT,
      country_code TEXT NOT NULL DEFAULT 'FR',
      contract_type TEXT,
      remote TEXT,
      salary_min REAL,
      salary_max REAL,
      salary_currency TEXT,
      posted_at TEXT,
      apply_url TEXT NOT NULL UNIQUE,
      description TEXT,
      first_seen_at TEXT NOT NULL,
      last_seen_at TEXT NOT NULL,
      is_active INTEGER NOT NULL DEFAULT 1,
      UNIQUE(source_type, source_id)
    );
    CREATE INDEX IF NOT EXISTS idx_jobs_active ON jobs(is_active, last_seen_at);
    CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company_key, is_active);
    CREATE INDEX IF NOT EXISTS idx_jobs_naf ON jobs(company_key);
    CREATE TABLE IF NOT EXISTS commoncrawl_index_runs (
      crawl_id TEXT NOT NULL,
      host TEXT NOT NULL,
      provider TEXT NOT NULL,
      harvested_at TEXT NOT NULL,
      tokens_found INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY(crawl_id, host)
    );
    CREATE TABLE IF NOT EXISTS source_runs (
      source_key TEXT PRIMARY KEY,
      last_checked_at TEXT,
      last_success_at TEXT,
      last_error TEXT
    );
    """)
    conn.commit()
    return conn


def _job_identity(source_type: str, source_id: str) -> str:
    return hashlib.sha256(f"{source_type}|{source_id}".encode("utf-8")).hexdigest()


def _job_dedupe(name: str, title: str, city: str | None, contract: str | None) -> str:
    value = "|".join((norm_text(name), norm_text(title), norm_text(city or "france"), norm_text(contract or "")))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def upsert_board(conn: sqlite3.Connection, board: dict) -> None:
    now = utc_now()
    key = f"{board['ats_type']}:{board['ats_slug']}"
    conn.execute("""
      INSERT INTO ats_boards (
        board_key, ats_type, ats_slug, board_url, eu_endpoint, discovery_source,
        seed_posting_count, first_seen_at, last_seen_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(board_key) DO UPDATE SET
        board_url=excluded.board_url,
        eu_endpoint=excluded.eu_endpoint,
        discovery_source=excluded.discovery_source,
        seed_posting_count=COALESCE(excluded.seed_posting_count, ats_boards.seed_posting_count),
        last_seen_at=excluded.last_seen_at
    """, (
        key, board["ats_type"], board["ats_slug"], board["board_url"],
        int(bool(board.get("eu_endpoint"))), board.get("discovery_source", "common_crawl_s3"),
        board.get("seed_posting_count"), now, now,
    ))


def upsert_company(conn: sqlite3.Connection, company: dict) -> None:
    now = utc_now()
    conn.execute("""
      INSERT INTO companies (
        company_key, name, website, ats_type, ats_slug, insee_match_status,
        first_seen_at, last_seen_at
      ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
      ON CONFLICT(company_key) DO UPDATE SET
        name=CASE WHEN excluded.name!='' THEN excluded.name ELSE companies.name END,
        website=COALESCE(excluded.website, companies.website),
        last_seen_at=excluded.last_seen_at
    """, (
        company["company_key"], company["name"], company.get("website"),
        company["ats_type"], company["ats_slug"], now, now,
    ))


def upsert_job(conn: sqlite3.Connection, company: dict, raw: dict) -> None:
    now = utc_now()
    source_type = raw["source_type"]
    source_id = str(raw["source_id"])
    job_key = _job_identity(source_type, source_id)
    city = raw.get("city")
    name = company["name"]
    dedupe_key = _job_dedupe(name, raw["title"], city, raw.get("contract_type"))
    url = raw["apply_url"]
    existing = conn.execute(
        "SELECT job_key, first_seen_at FROM jobs WHERE job_key=? OR apply_url=? OR (source_type=? AND source_id=?) LIMIT 1",
        (job_key, url, source_type, source_id),
    ).fetchone()
    first_seen = existing["first_seen_at"] if existing else now
    if existing:
        job_key = existing["job_key"]
    conn.execute("""
      INSERT INTO jobs (
        job_key, dedupe_key, company_key, source_type, source_id, title, department,
        location, city, country_code, contract_type, remote, salary_min, salary_max,
        salary_currency, posted_at, apply_url, description, first_seen_at, last_seen_at, is_active
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'FR', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
      ON CONFLICT(job_key) DO UPDATE SET
        dedupe_key=excluded.dedupe_key,
        company_key=excluded.company_key,
        title=excluded.title,
        department=excluded.department,
        location=excluded.location,
        city=excluded.city,
        contract_type=excluded.contract_type,
        remote=excluded.remote,
        salary_min=excluded.salary_min,
        salary_max=excluded.salary_max,
        salary_currency=excluded.salary_currency,
        posted_at=COALESCE(excluded.posted_at, jobs.posted_at),
        apply_url=excluded.apply_url,
        description=CASE WHEN excluded.description!='' THEN excluded.description ELSE jobs.description END,
        last_seen_at=excluded.last_seen_at,
        is_active=1
    """, (
        job_key, dedupe_key, company["company_key"], source_type, source_id,
        raw["title"], raw.get("department"), raw.get("location"), city,
        raw.get("contract_type"), raw.get("remote"), raw.get("salary_min"),
        raw.get("salary_max"), raw.get("salary_currency"), raw.get("posted_at"),
        url, raw.get("description", "")[:12000], first_seen, now,
    ))


def deactivate_unseen_jobs(conn: sqlite3.Connection, company_key: str, source_type: str, run_started: str) -> None:
    # Only call after a full, successful board response. Failed/partial feeds must
    # never close jobs by accident.
    conn.execute("""
      UPDATE jobs SET is_active=0
      WHERE company_key=? AND source_type=? AND last_seen_at < ?
    """, (company_key, source_type, run_started))


def store_board_result(
    conn: sqlite3.Connection, board: dict, raw_jobs: list[dict], run_started: str,
    *, deactivate: bool = True, update_board: bool = True,
) -> int:
    """Persist one complete public ATS board response.

    WTTJ is a global index, so each hit supplies its own company identity and
    stale WTTJ jobs are closed only after the complete global feed succeeds.
    """
    board_key = f"{board['ats_type']}:{board['ats_slug']}"
    is_live_fr = False
    kept = 0
    for raw in raw_jobs:
        if not raw.get("title") or not raw.get("apply_url"):
            continue
        in_france, city = is_france_location(raw.get("location", ""), raw.get("country_code"))
        if not in_france:
            continue
        company_key = raw.get("company_key") or board_key
        company_ats_type = raw.get("company_ats_type") or board["ats_type"]
        company_ats_slug = raw.get("company_ats_slug") or board["ats_slug"]
        company_name = (raw.get("company_name") or board.get("company_name")
                        or company_name_from_slug(company_ats_slug))
        company = {
            "company_key": company_key, "name": company_name,
            "website": raw.get("company_website") or board.get("company_website"),
            "ats_type": company_ats_type, "ats_slug": company_ats_slug,
        }
        upsert_company(conn, company)
        raw["source_type"] = raw.get("source_type") or board["ats_type"]
        source_id = raw.get("source_id") or raw["apply_url"]
        raw["source_id"] = f"{company_key}:{source_id}"
        raw["city"] = raw.get("city") or city
        if not raw["city"] and raw.get("location"):
            raw["city"] = next(
                (name.title() for name in FR_CITIES if re.search(rf"\b{re.escape(name)}\b", norm_text(raw["location"]))),
                None,
            )
        raw["remote"] = "remote" if raw.get("remote") else ""
        upsert_job(conn, company, raw)
        is_live_fr = True
        kept += 1

    if update_board:
        conn.execute("""
          UPDATE ats_boards SET
            last_probed_at=?, last_success_at=?, last_error=NULL, has_fr_jobs=?
          WHERE board_key=?
        """, (run_started, utc_now(), int(is_live_fr), board_key))
    if deactivate:
        # A role is closed only after a complete, successful board response.
        deactivate_unseen_jobs(conn, board_key, board["ats_type"], run_started)
    return kept


def mark_board_error(conn: sqlite3.Connection, board: dict, error: str, run_started: str) -> None:
    key = f"{board['ats_type']}:{board['ats_slug']}"
    conn.execute("""
      UPDATE ats_boards SET last_probed_at=?, last_error=? WHERE board_key=?
    """, (run_started, error[:500], key))


def _raw_job(
    source_type: str, source_id: Any, title: Any, url: Any,
    location: Any = "", country_code: Any = None, **fields
) -> dict | None:
    title = str(title or "").strip()
    url = str(url or "").strip()
    if not title or not url.startswith("https://"):
        return None
    return {
        "source_type": source_type, "source_id": str(source_id or url),
        "title": title, "apply_url": url, "location": str(location or ""),
        "country_code": str(country_code or "").upper() or None,
        "department": fields.get("department"),
        "contract_type": fields.get("contract_type"),
        "remote": bool(fields.get("remote")),
        "salary_min": fields.get("salary_min"),
        "salary_max": fields.get("salary_max"),
        "salary_currency": fields.get("salary_currency"),
        "posted_at": parse_date(fields.get("posted_at")),
        "description": strip_html(fields.get("description")),
        "company_name": fields.get("company_name"),
        "company_website": fields.get("company_website"),
    }


async def _robots_allowed(http: "PoliteHTTP", url: str) -> bool:
    return await http.robots_allowed(url)


async def fetch_ats_board(http: "PoliteHTTP", board: dict) -> tuple[list[dict], str | None]:
    """Read one discovered public ATS board. Returns (jobs, error)."""
    provider = board["ats_type"]
    slug = urllib.parse.quote(str(board["ats_slug"]), safe="|._-")
    try:
        if provider == "recruitee" and date.today() >= RECRUITEE_TOKEN_DATE:
            return [], "Recruitee public offers endpoint is scheduled to require an employer token from 2027-02-10"
        if provider == "greenhouse":
            url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
        elif provider == "lever":
            api_host = "api.eu.lever.co" if board.get("eu_endpoint") else "api.lever.co"
            url = f"https://{api_host}/v0/postings/{slug}?mode=json"
        elif provider == "ashby":
            url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
        elif provider == "workable":
            url = f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"
        elif provider == "breezy":
            url = f"https://{slug}.breezy.hr/json"
        elif provider == "recruitee":
            url = f"https://{slug}.recruitee.com/api/offers/"
        elif provider == "teamtailor":
            url = f"https://{slug}.teamtailor.com/jobs.rss"
        elif provider == "taleez":
            url = f"https://{slug}.taleez.com/api/careez"
        elif provider == "smartrecruiters":
            url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100&country=FR"
        else:
            return [], f"Unsupported public ATS: {provider}"

        if not await _robots_allowed(http, url):
            return [], f"robots.txt disallows the exact public feed path: {url}"

        if provider == "greenhouse":
            payload = await http.get_json(url)
            if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
                raise ValueError("Greenhouse returned an unexpected board response")
            results = []
            for item in payload["jobs"]:
                offices = item.get("offices") or []
                loc = (item.get("location") or {}).get("name", "")
                if not loc and offices:
                    loc = ", ".join(str(x.get("name") or x.get("location") or "") for x in offices)
                results.append(_raw_job(
                    provider, item.get("id"), item.get("title"), item.get("absolute_url"),
                    loc, item.get("country_code"),
                    department=(item.get("departments") or [{}])[0].get("name"),
                    posted_at=item.get("first_published") or item.get("updated_at"),
                    description=item.get("content"), company_name=item.get("company_name"),
                ))
            return [x for x in results if x], None

        if provider == "lever":
            payload = await http.get_json(url)
            if not isinstance(payload, list):
                raise ValueError("Lever returned an unexpected board response")
            results = []
            for item in payload:
                cats = item.get("categories") or {}
                locations = cats.get("allLocations") or []
                loc = cats.get("location") or ", ".join(locations)
                workplace = str(item.get("workplaceType") or "").lower()
                results.append(_raw_job(
                    provider, item.get("id"), item.get("text"),
                    item.get("hostedUrl") or item.get("applyUrl"), loc,
                    item.get("countryCode"),
                    department=cats.get("team") or cats.get("department"),
                    posted_at=item.get("createdAt"),
                    description=item.get("descriptionPlain") or item.get("description"),
                    remote=workplace == "remote",
                    contract_type=item.get("commitment"),
                ))
            return [x for x in results if x], None

        if provider == "ashby":
            payload = await http.get_json(url)
            if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
                raise ValueError("Ashby returned an unexpected board response")
            results = []
            for item in payload["jobs"]:
                if item.get("isListed") is False:
                    continue
                address = (item.get("address") or {}).get("postalAddress") or {}
                location = item.get("location") or ""
                if address.get("addressLocality") and address["addressLocality"] not in location:
                    location = f"{location}, {address['addressLocality']}".strip(", ")
                if address.get("addressCountry") and address["addressCountry"] not in location:
                    location = f"{location}, {address['addressCountry']}".strip(", ")
                comp = (item.get("compensation") or {}).get("compensationTierSummary") or ""
                results.append(_raw_job(
                    provider, item.get("id") or item.get("jobUrl"), item.get("title"),
                    item.get("applyUrl") or item.get("jobUrl"), location,
                    address.get("addressCountry"),
                    department=item.get("department") or item.get("team"),
                    posted_at=item.get("publishedAt"),
                    description=item.get("descriptionPlain") or item.get("descriptionHtml") or comp,
                    remote=str(item.get("workplaceType") or "").lower() == "remote",
                    contract_type=item.get("employmentType"),
                ))
            return [x for x in results if x], None

        if provider == "workable":
            payload = await http.get_json(url, headers={"Origin": "https://apply.workable.com"})
            if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
                raise ValueError("Workable returned an unexpected board response")
            results = []
            for item in payload["jobs"]:
                location = ", ".join(str(v) for v in (
                    item.get("city"), item.get("state"), item.get("country")
                ) if v)
                results.append(_raw_job(
                    provider, item.get("shortcode") or item.get("code"), item.get("title"),
                    item.get("application_url") or item.get("url"), location,
                    item.get("country_code"),
                    department=item.get("department"),
                    posted_at=item.get("published_on") or item.get("created_at"),
                    description=item.get("description"),
                    remote=item.get("telecommuting"),
                    contract_type=item.get("employment_type"),
                    company_name=item.get("company_name") or payload.get("name"),
                    company_website=payload.get("website"),
                ))
            return [x for x in results if x], None

        if provider == "breezy":
            payload = await http.get_json(url)
            rows = payload if isinstance(payload, list) else (
                payload.get("jobs") or payload.get("positions") if isinstance(payload, dict) else None
            )
            if not isinstance(rows, list):
                raise ValueError("Breezy returned an unexpected board response")
            results = []
            for item in rows:
                location = item.get("location") or item.get("locations") or ""
                if isinstance(location, dict):
                    location = ", ".join(str(v) for v in (
                        location.get("city"), location.get("region"), location.get("country")
                    ) if v)
                results.append(_raw_job(
                    provider, item.get("id") or item.get("position_id"),
                    item.get("name") or item.get("title"),
                    item.get("url") or item.get("career_url") or (
                        f"https://{slug}.breezy.hr/p/{item.get('id')}"
                    ), str(location), item.get("country_code"),
                    department=item.get("department") or item.get("team"),
                    posted_at=item.get("published_date") or item.get("published_at") or item.get("created_at"),
                    description=item.get("description"),
                    remote=item.get("remote") or item.get("is_remote"),
                    company_name=item.get("company_name"),
                ))
            return [x for x in results if x], None

        if provider == "recruitee":
            payload = await http.get_json(url)
            if not isinstance(payload, dict) or not isinstance(payload.get("offers"), list):
                raise ValueError("Recruitee returned an unexpected board response")
            results = []
            for item in payload["offers"]:
                if item.get("status") not in (None, "published"):
                    continue
                location = ", ".join(str(v) for v in (
                    item.get("city"), item.get("country")
                ) if v) or item.get("location") or ""
                results.append(_raw_job(
                    provider, item.get("id"), item.get("title"),
                    item.get("careers_url") or item.get("url"), location,
                    item.get("country_code"),
                    department=item.get("department"),
                    posted_at=item.get("published_at") or item.get("created_at"),
                    description=item.get("description"),
                    remote=item.get("remote") or item.get("workplace") == "remote",
                    company_name=payload.get("company_name") or payload.get("name"),
                ))
            return [x for x in results if x], None

        if provider == "teamtailor":
            response = await http.get_text(url)
            feed = feedparser.parse(response)
            if feed.bozo and not feed.entries:
                raise ValueError("Teamtailor returned invalid RSS")
            company_name = (feed.feed.get("title") if feed.feed else None) or None
            results = []
            for item in feed.entries:
                values = {str(k).lower(): v for k, v in item.items()}
                location_parts = []
                for key, value in values.items():
                    if "city" in key or "country" in key:
                        if isinstance(value, str) and value.strip() and value not in location_parts:
                            location_parts.append(value.strip())
                location = ", ".join(location_parts)
                remote_status = str(values.get("tt_remotestatus") or values.get("remote_status") or "").lower()
                results.append(_raw_job(
                    provider, item.get("id") or item.get("guid") or item.get("link"),
                    item.get("title"), item.get("link"), location,
                    item.get("country_code"),
                    department=item.get("tt_department") or item.get("department"),
                    posted_at=item.get("published") or item.get("updated"),
                    description=item.get("summary") or item.get("description"),
                    remote=remote_status in {"fully", "remote", "temporary"},
                    company_name=company_name,
                ))
            return [x for x in results if x], None

        if provider == "smartrecruiters":
            results = []
            offset = 0
            total = None
            while offset < 5000:
                page_url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100&offset={offset}&country=FR"
                if not await _robots_allowed(http, page_url):
                    raise PermissionError("SmartRecruiters API robots policy disallows public postings")
                payload = await http.get_json(page_url)
                if not isinstance(payload, dict) or not isinstance(payload.get("content"), list):
                    raise ValueError("SmartRecruiters returned an unexpected public postings response")
                items = payload["content"]
                for item in items:
                    location = item.get("location") or {}
                    country_value = str(location.get("country") or "").strip()
                    country_code = "FR" if norm_text(country_value) == "france" else (
                        country_value.upper() if re.fullmatch(r"[A-Za-z]{2}", country_value) else None
                    )
                    results.append(_raw_job(
                        provider, item.get("id"), item.get("name"),
                        f"https://jobs.smartrecruiters.com/{slug}/{item.get('id')}",
                        location.get("fullLocation") or ", ".join(
                            str(v) for v in (location.get("city"), location.get("country")) if v
                        ),
                        country_code,
                        department=(item.get("department") or {}).get("label") if isinstance(item.get("department"), dict) else item.get("department"),
                        posted_at=item.get("releasedDate"),
                        description=item.get("jobAd") or item.get("description") or "",
                        remote=location.get("remote"),
                    ))
                try:
                    total = int(payload.get("totalFound", offset + len(items)))
                except (TypeError, ValueError):
                    total = offset + len(items)
                offset += len(items)
                if not items or offset >= total:
                    break
            return [item for item in results if item], None

        if provider == "taleez":
            payload = await http.get_json(url)
            if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
                raise ValueError("Taleez returned an unexpected career-site response")
            company_name = payload.get("name") or company_name_from_slug(slug)
            results = []
            for item in payload["jobs"]:
                loc = item.get("location") or {}
                location = ", ".join(str(v) for v in (
                    loc.get("city"), loc.get("region"), loc.get("country")
                ) if v)
                apply_slug = urllib.parse.quote(str(item.get("slug") or ""), safe="-_")
                if not apply_slug:
                    continue
                results.append(_raw_job(
                    provider, item.get("id"), item.get("label"),
                    f"https://taleez.com/apply/{apply_slug}", location, loc.get("country"),
                    posted_at=item.get("creationDate") or item.get("publishDate"),
                    description=item.get("description"),
                    remote=item.get("remote"),
                    contract_type=item.get("contract"),
                    company_name=company_name,
                ))
            return [x for x in results if x], None

    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"
    return [], f"Unsupported ATS: {provider}"


def parse_wttj_env(value: str) -> tuple[str, str]:
    start, end = value.find("{"), value.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("WTTJ public environment payload contained no JSON object")
    payload = json.loads(value[start:end + 1])
    app_id = str(payload.get("PUBLIC_ALGOLIA_APPLICATION_ID") or "").strip()
    key = str(payload.get("PUBLIC_ALGOLIA_API_KEY_CLIENT") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9]{6,16}", app_id) or not (16 <= len(key) <= 500):
        raise ValueError("WTTJ public Algolia client settings had an unexpected shape")
    return app_id, key


def normalise_wttj_hit(hit: dict) -> dict | None:
    name = str(hit.get("name") or "").strip()
    job_slug = str(hit.get("slug") or "").strip()
    org = hit.get("organization") or {}
    org_slug = str(org.get("slug") or "").strip()
    company_name = str(org.get("name") or "").strip()
    if not name or not job_slug or not org_slug or not company_name:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]+", job_slug) or not re.fullmatch(r"[A-Za-z0-9_-]+", org_slug):
        return None

    offices = hit.get("offices") or []
    office = offices[0] if offices else {}
    location = ", ".join(str(x) for x in (
        office.get("city"), office.get("state"), office.get("country")
    ) if x)
    country_code = str(office.get("country_code") or "").upper() or None
    if hit.get("remote") == "fulltime":
        location = (location + ", Remote").strip(", ")
    raw = _raw_job(
        "wttj", hit.get("id") or job_slug, name,
        f"{WTTJ_ORIGIN}/fr/companies/{org_slug}/jobs/{job_slug}",
        location, country_code,
        department=(hit.get("new_profession") or {}).get("name") or hit.get("department"),
        posted_at=hit.get("published_at_timestamp"),
        description=hit.get("description") or hit.get("summary") or "",
        remote=hit.get("remote") == "fulltime",
        contract_type=hit.get("contract_type"),
        salary_min=hit.get("salary_yearly_minimum"),
        salary_max=hit.get("salary_maximum") if hit.get("salary_period") == "yearly" else None,
        salary_currency=hit.get("salary_currency"),
        company_name=company_name,
        company_website=org.get("website"),
    )
    if raw:
        raw["company_key"] = f"wttj:{org_slug}"
        raw["company_ats_type"] = "wttj"
        raw["company_ats_slug"] = org_slug
        raw["source_type"] = "wttj"
        raw["source_id"] = f"{org_slug}:{hit.get('id') or job_slug}"
    return raw


async def fetch_wttj_france_jobs(http: "PoliteHTTP") -> tuple[list[dict], str | None]:
    """Read WTTJ's public frontend index, restricted server-side to France.

    This is a public frontend search interface, not a vendor-published third-party
    API. The key is the public client key served by /api/env; it is never stored.
    """
    try:
        if not await http.robots_allowed(WTTJ_ENV_URL):
            return [], "WTTJ robots.txt disallows its public environment endpoint"
        env_text = await http.get_text(WTTJ_ENV_URL)
        app_id, key = parse_wttj_env(env_text)
        algolia_host = f"{app_id}-dsn.algolia.net"
        query_url = f"https://{algolia_host}/1/indexes/{WTTJ_INDEX}/query"
        if not await http.robots_allowed(query_url):
            return [], "Algolia robots.txt disallows WTTJ public search index"
        jobs: list[dict] = []
        page = 0
        total_pages = 1
        while page < total_pages:
            params = {
                "query": "",
                "hitsPerPage": "1000",
                "page": str(page),
                "filters": "offices.country_code:FR",
                "attributesToRetrieve": WTTJ_ATTRS,
            }
            response = await http.post_json(
                query_url, {"params": urllib.parse.urlencode(params)},
                headers={
                    "x-algolia-application-id": app_id,
                    "x-algolia-api-key": key,
                    "referer": WTTJ_ORIGIN + "/",
                    "content-type": "application/json",
                },
            )
            if not isinstance(response, dict) or not isinstance(response.get("hits"), list):
                raise ValueError("WTTJ Algolia response did not contain hits[]")
            total_pages = max(1, int(response.get("nbPages") or 1))
            if total_pages > 100:
                raise ValueError(f"WTTJ returned {total_pages} pages; refusing to expire data after an incomplete scan")
            for hit in response["hits"]:
                job = normalise_wttj_hit(hit)
                if job:
                    jobs.append(job)
            page += 1
            if page < total_pages:
                await asyncio.sleep(0.25)
        return jobs, None
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"


class PoliteHTTP:
    def __init__(self, timeout: float = 25.0):
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0),
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/xml, application/rss+xml, */*"},
            limits=httpx.Limits(max_connections=30, max_keepalive_connections=15),
        )
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last: dict[str, float] = defaultdict(float)
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    async def close(self) -> None:
        await self.client.aclose()

    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        host = httpx.URL(url).host or ""
        delay = HOST_DELAY.get(host, 0.25)
        async with self._locks[host]:
            wait = delay - (time.monotonic() - self._last[host])
            if wait > 0:
                await asyncio.sleep(wait)
            self._last[host] = time.monotonic()
            last_error: Exception | None = None
            for attempt in range(4):
                try:
                    response = await self.client.request(method, url, **kwargs)
                    if response.status_code in (429, 500, 502, 503, 504):
                        retry_after = response.headers.get("Retry-After")
                        sleep_for = float(retry_after) if retry_after and retry_after.isdigit() else min(2 ** attempt, 20)
                        if attempt == 3:
                            response.raise_for_status()
                        await asyncio.sleep(sleep_for)
                        continue
                    if response.status_code in (301, 302, 303, 307, 308):
                        raise httpx.HTTPStatusError(
                            f"Refusing redirect for {url}", request=response.request, response=response
                        )
                    response.raise_for_status()
                    return response
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    last_error = exc
                    if attempt == 3:
                        raise
                    await asyncio.sleep(min(2 ** attempt, 10))
            if last_error:
                raise last_error
            raise RuntimeError(f"Request failed: {url}")

    async def get_json(self, url: str, **kwargs) -> Any:
        return (await self.request("GET", url, **kwargs)).json()

    async def get_text(self, url: str, **kwargs) -> str:
        return (await self.request("GET", url, **kwargs)).text

    async def post_json(self, url: str, payload: dict, **kwargs) -> Any:
        response = await self.request("POST", url, json=payload, **kwargs)
        return response.json()

    async def robots_allowed(self, url: str) -> bool:
        parsed = httpx.URL(url)
        base = f"{parsed.scheme}://{parsed.host}"
        if base not in self._robots:
            robots_url = base + "/robots.txt"
            try:
                response = await self.request("GET", robots_url)
                if response.status_code in (404, 410) or 400 <= response.status_code < 500:
                    # RFC 9309: an unavailable 4xx robots file is not a blanket ban.
                    self._robots[base] = None
                elif response.status_code >= 500:
                    self._robots[base] = False
                else:
                    parser = urllib.robotparser.RobotFileParser()
                    parser.parse(response.text.splitlines())
                    self._robots[base] = parser
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if 400 <= status < 500 and status not in (429,):
                    # RFC 9309: an unavailable (4xx) robots file is not a blanket ban.
                    self._robots[base] = None
                else:
                    # A server error or rate limit is fail-closed; try this source next run.
                    self._robots[base] = False
            except Exception:
                self._robots[base] = False
        parser = self._robots[base]
        if parser is False:
            return False
        return True if parser is None else parser.can_fetch(USER_AGENT, url)


def _legal_name_key(value: str) -> str:
    words = norm_text(value).split()
    words = [word for word in words if word not in LEGAL_SUFFIXES]
    return " ".join(words)


def insee_match_score(company_name: str, candidate: dict) -> float:
    target = _legal_name_key(company_name)
    names = [
        candidate.get("nom_complet"),
        candidate.get("nom_raison_sociale"),
        candidate.get("denomination"),
    ]
    alternatives = candidate.get("matching_etablissements") or []
    for establishment in alternatives:
        names.extend(establishment.get("liste_enseignes") or [])
    scores = [SequenceMatcher(None, target, _legal_name_key(str(name))).ratio()
              for name in names if name]
    return max(scores, default=0.0)


def choose_insee_result(company_name: str, results: list[dict]) -> tuple[dict | None, float]:
    candidates = []
    for item in results:
        if not isinstance(item, dict):
            continue
        if str(item.get("etat_administratif") or "") != "A":
            continue
        if str(item.get("nature_juridique") or "") == "1000":
            continue
        if str(item.get("statut_diffusion") or "") != "O":
            continue
        score = insee_match_score(company_name, item)
        if score >= 0.90:
            candidates.append((score, item))
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    if not candidates:
        return None, 0.0
    if len(candidates) > 1 and candidates[0][0] - candidates[1][0] < 0.04:
        # Similar names across two different legal entities: leave unresolved.
        return None, candidates[0][0]
    return candidates[0][1], candidates[0][0]


async def enrich_company_insee(http: PoliteHTTP, conn: sqlite3.Connection, company_key: str) -> bool:
    company = conn.execute("SELECT * FROM companies WHERE company_key=?", (company_key,)).fetchone()
    if not company or company["insee_match_status"] == "matched":
        return False
    name = company["name"]
    try:
        if not await http.robots_allowed(INSEE_URL):
            raise RuntimeError("INSEE search API robots policy disallows the query endpoint")
        payload = await http.get_json(INSEE_URL, params={"q": name, "per_page": 10, "etat_administratif": "A"})
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            raise ValueError("INSEE search response did not contain results[]")
        match, score = choose_insee_result(name, results)
        if not match:
            conn.execute("""
              UPDATE companies SET insee_match_status='unmatched',
                insee_match_score=?, insee_checked_at=? WHERE company_key=?
            """, (score, utc_now(), company_key))
            return False
        siren = str(match.get("siren") or "")
        if not re.fullmatch(r"\d{9}", siren):
            conn.execute("""
              UPDATE companies SET insee_match_status='unmatched',
                insee_match_score=?, insee_checked_at=? WHERE company_key=?
            """, (score, utc_now(), company_key))
            return False
        establishments = match.get("matching_etablissements") or []
        headquarters = next((e for e in establishments if e.get("est_siege") or e.get("siege_social")), None)
        headquarters = headquarters or (establishments[0] if establishments else {})
        naf25 = headquarters.get("activite_principale_naf25") or match.get("activite_principale_naf25")
        naf_legacy = headquarters.get("activite_principale") or match.get("activite_principale")
        if isinstance(naf25, dict):
            naf25 = naf25.get("code")
        if isinstance(naf_legacy, dict):
            naf_legacy = naf_legacy.get("code")
        label = (
            (headquarters.get("activite_principale_naf25") or {}).get("libelle")
            if isinstance(headquarters.get("activite_principale_naf25"), dict) else None
        ) or (
            (match.get("activite_principale_naf25") or {}).get("libelle")
            if isinstance(match.get("activite_principale_naf25"), dict) else None
        ) or headquarters.get("libelle_activite_principale") or match.get("libelle_activite_principale") or ""
        city = headquarters.get("libelle_commune") or match.get("libelle_commune") or ""
        conn.execute("""
          UPDATE companies SET siren=?, legal_name=?, naf_code=?, naf_code_rev2=?,
            naf_label=?, legal_city=?, legal_form=?, insee_match_status='matched',
            insee_match_score=?, insee_checked_at=? WHERE company_key=?
        """, (
            siren, match.get("nom_complet") or match.get("nom_raison_sociale") or name,
            naf25 or naf_legacy or None, naf_legacy or None, label, city,
            str(match.get("nature_juridique") or ""), score, utc_now(), company_key,
        ))
        return True
    except Exception as exc:
        conn.execute("""
          UPDATE companies SET insee_match_status='error',
            insee_match_score=NULL, insee_checked_at=?, legal_name=COALESCE(legal_name, name)
          WHERE company_key=?
        """, (utc_now(), company_key))
        print(f"INSEE enrichment failed for {name}: {exc}")
        return False


def export_data(conn: sqlite3.Connection, generated_at: str | None = None) -> dict:
    generated_at = generated_at or utc_now()
    rows = conn.execute("""
      SELECT j.*, c.name AS company_name, c.website, c.siren, c.legal_name,
             c.naf_code, c.naf_code_rev2, c.naf_label, c.legal_city,
             c.insee_match_status
      FROM jobs j
      JOIN companies c ON c.company_key=j.company_key
      WHERE j.is_active=1
      ORDER BY COALESCE(j.posted_at, j.first_seen_at) DESC, c.name, j.title
    """).fetchall()
    selected: dict[str, dict] = {}
    for row in rows:
        item = dict(row)
        # De-duplicate the same role syndicated through WTTJ and an ATS feed.
        key = item["dedupe_key"]
        prior = selected.get(key)
        if prior is None or (prior["source_type"] == "wttj" and item["source_type"] != "wttj"):
            selected[key] = item
    jobs = []
    naf_groups: dict[str, list[dict]] = defaultdict(list)
    for row in selected.values():
        lat, lon = coordinates(row.get("city"))
        job = {
            "id": row["job_key"],
            "company": row["company_name"],
            "company_slug": row["company_key"],
            "title": row["title"],
            "url": row["apply_url"],
            "location": row.get("location") or row.get("city") or "France",
            "city": row.get("city") or "France",
            "country": "FR",
            "lat": lat, "lon": lon,
            "remote": row.get("remote") == "remote",
            "role": classify_role(row["title"]),
            "level": classify_level(row["title"]),
            "language": "",
            "salary": ({
                "min": row.get("salary_min"), "max": row.get("salary_max"),
                "currency": row.get("salary_currency") or "EUR",
            } if row.get("salary_min") or row.get("salary_max") else None),
            "department": row.get("department"),
            "posted": row.get("posted_at"),
            "snippet": row.get("description") or "",
            "first_seen": row.get("first_seen_at"),
            "last_seen": row.get("last_seen_at"),
            "ats": row.get("source_type"),
            "siren": row.get("siren"),
            "naf_code": row.get("naf_code"),
            "naf_code_rev2": row.get("naf_code_rev2"),
            "naf_label": row.get("naf_label"),
            "insee_match_status": row.get("insee_match_status"),
        }
        jobs.append(job)
        naf_key = row.get("naf_code") or "unclassified"
        naf_groups[naf_key].append(job)
    data = {
        "generated_at": generated_at,
        "count": len(jobs),
        "companies_count": len({job["company_slug"] for job in jobs}),
        "companies_matched_to_insee": len({job["company_slug"] for job in jobs if job.get("siren")}),
        "naf_groups": {key: values for key, values in sorted(naf_groups.items())},
        "jobs": jobs,
    }
    return data


def write_exports(conn: sqlite3.Connection, json_path: Path = JSON_DEFAULT,
                  csv_path: Path = CSV_DEFAULT, site_path: Path = SITE_JSON_DEFAULT,
                  report: dict | None = None) -> dict:
    data = export_data(conn)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    site_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    csv_fields = [
        "id", "title", "company", "siren", "naf_code", "naf_code_rev2",
        "naf_label", "city", "location", "contract_type", "remote",
        "salary_min", "salary_max", "salary_currency", "posted",
        "apply_url", "source_type", "insee_match_status",
    ]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=csv_fields, extrasaction="ignore")
        writer.writeheader()
        for row in conn.execute("""
          SELECT j.job_key AS id, j.title, c.name AS company, c.siren,
                 c.naf_code, c.naf_code_rev2, c.naf_label, j.city, j.location,
                 j.contract_type, j.remote, j.salary_min, j.salary_max,
                 j.salary_currency, j.posted_at AS posted, j.apply_url,
                 j.source_type, c.insee_match_status
          FROM jobs j JOIN companies c ON c.company_key=j.company_key
          WHERE j.is_active=1 ORDER BY COALESCE(j.posted_at,j.first_seen_at) DESC
        """):
            writer.writerow(dict(row))
    site_data = {
        "generated_at": data["generated_at"], "count": data["count"],
        "demo": False, "jobs": data["jobs"],
    }
    site_path.write_text(json.dumps(site_data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    if report is not None:
        REPORT_DEFAULT.parent.mkdir(parents=True, exist_ok=True)
        REPORT_DEFAULT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return data
