"""Shared public ATS readers, France filters and SQLite persistence."""
from __future__ import annotations

import asyncio
import csv
import gzip
import html
import json
import re
import sqlite3
import time
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse
from urllib.robotparser import RobotFileParser

import feedparser
import httpx

COMMONCRAWL_BASE = "https://data.commoncrawl.org/cc-index/collections"
COMMONCRAWL_META_URL = "https://index.commoncrawl.org/collinfo.json"
SEED_DATA_URL = "https://raw.githubusercontent.com/veresk06/open-ats-feed/main/actor/data/companies.json"
RECRUITEE_TOKEN_DATE = date(2027, 2, 10)

# Anonymous public job-board reads. Private recruiting APIs are intentionally excluded.
# SmartRecruiters and Personio are omitted because their relevant public feed paths
# are disallowed by the robots policies checked against live and archived files.
PROVIDERS: dict[str, dict[str, Any]] = {
    "greenhouse": {"hosts": [("boards.greenhouse.io", "path"), ("job-boards.greenhouse.io", "path"),
                             ("boards.eu.greenhouse.io", "path"), ("job-boards.eu.greenhouse.io", "path")],
                   "concurrency": 8, "interval": 0.18},
    "lever": {"hosts": [("jobs.lever.co", "path"), ("jobs.eu.lever.co", "path")],
              "concurrency": 1, "interval": 1.0},
    "ashby": {"hosts": [("jobs.ashbyhq.com", "path")], "concurrency": 12, "interval": 0.12},
    "workable": {"hosts": [("apply.workable.com", "path")], "concurrency": 1, "interval": 1.0},
    "breezy": {"hosts": [("breezy.hr", "subdomain")], "concurrency": 8, "interval": 0.12},
    "recruitee": {"hosts": [("recruitee.com", "subdomain")], "concurrency": 8, "interval": 0.12},
    "teamtailor": {"hosts": [("teamtailor.com", "subdomain")], "concurrency": 8, "interval": 0.12},
    "gem": {"hosts": [("jobs.gem.com", "path")], "concurrency": 10, "interval": 0.1},
    "pinpoint": {"hosts": [("pinpointhq.com", "subdomain")], "concurrency": 8, "interval": 0.12},
    "bamboohr": {"hosts": [("bamboohr.com", "subdomain")], "concurrency": 8, "interval": 0.12},
    "rippling": {"hosts": [("ats.rippling.com", "path")], "concurrency": 8, "interval": 0.12},
    "workday": {"hosts": [("myworkdayjobs.com", "workday")], "concurrency": 5, "interval": 0.2},
}
SEED_PROVIDERS = {"greenhouse", "lever", "ashby", "breezy", "recruitee", "teamtailor"}
RESERVED_SLUGS = {
    "www", "api", "jobs", "careers", "career", "apply", "embed", "static",
    "assets", "cdn", "login", "signup", "help", "support", "blog", "status",
    "admin", "dashboard", "account", "oauth", "search", "postings", "job",
    "j", "company", "companies", "feed", "docs", "developers", "developer",
    "v0", "v1", "robots.txt", "sitemap.xml", "index.html", "favicon.ico",
    "error", "404", "terms", "privacy",
}
FR_CITIES = {
    "paris", "lyon", "marseille", "toulouse", "nice", "nantes", "montpellier",
    "strasbourg", "bordeaux", "lille", "rennes", "reims", "saint etienne",
    "le havre", "toulon", "grenoble", "dijon", "angers", "nimes", "villeurbanne",
    "clermont ferrand", "aix en provence", "tours", "amiens", "metz", "perpignan",
    "orleans", "mulhouse", "rouen", "caen", "nancy", "saint denis", "annecy",
    "la rochelle", "lorient", "brest", "limoges", "besancon", "poitiers", "pau",
    "avignon", "versailles", "boulogne billancourt", "cannes", "antibes", "colmar",
    "chambery", "valence", "bourges", "quimper", "bayonne", "biarritz", "courbevoie",
    "issy les moulineaux", "levallois perret", "neuilly sur seine", "nanterre",
    "puteaux", "saint ouen", "cergy", "creteil", "evry", "meudon",
}
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
PHONE_RE = re.compile(
    r"(?<!\w)(?:\+\d{1,3}[\s()./-]?)?(?:\(?0?\d{1,3}\)?[\s()./-]?)"
    r"\d{2,4}(?:[\s()./-]?\d{2,4}){2,3}(?!\w)"
)
HTML_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")
REMOTE_RE = re.compile(r"\b(remote|télétravail|teletravail|home office)\b", re.I)

ROLE_RULES = [
    ("data", r"data scientist|data engineer|data analyst|machine learning|\bml\b|\bai\b|analytics"),
    ("engineering", r"engineer|developer|software|devops|backend|frontend|full.?stack|security|qa\b|ingénieur|ingenieur|développeur|developpeur"),
    ("product", r"product manager|product owner|product lead|chef de produit"),
    ("design", r"design|ux|ui\b|creative|graphiste"),
    ("sales", r"sales|account executive|account manager|business development|\bbdr\b|\bsdr\b|commercial|partnership"),
    ("marketing", r"marketing|growth|content|seo|communications|social media"),
    ("support", r"customer success|customer support|client success|support specialist|service client"),
    ("finance", r"finance|accountant|accounting|controller|tax|audit|risk|treasury|comptable"),
    ("legal", r"legal|lawyer|compliance|privacy officer|juriste"),
    ("people", r"recruit|talent|people|\bhr\b|human resources|recruteur|ressources humaines"),
    ("procurement", r"procurement|purchasing|buyer|einkauf|inkoop"),
    ("admin", r"administrative|administrator|office manager|executive assistant|assistant administratif"),
    ("business", r"business analyst|management consultant|strategy consultant|business operations|consultant"),
    ("operations", r"operations|supply chain|logistics|project manager|program manager|service delivery"),
    ("healthcare", r"physician|doctor|nurse|pharmacist|clinical|medical|médecin|infirmier"),
    ("education", r"teacher|lecturer|professor|academic|education|enseignant|professeur"),
    ("leadership", r"\bceo\b|\bcfo\b|\bcoo\b|\bcto\b|chief executive|director|directeur général"),
]
LEVEL_RULES = [
    ("intern", r"\bintern(ship)?\b|\btrainee\b|\bstagiaire\b|\bstage\b|\bapprentice\b|\balternance\b"),
    ("junior", r"\bjunior\b|\bgraduate\b|entry.?level|\bassociate\b"),
    ("lead", r"\b(lead|staff|principal|head of|director|vp|chief)\b|\bmanager\b"),
    ("senior", r"\bsenior\b|\bsr\b|\bexperienced\b"),
]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def norm_text(value: Any) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKD", str(value).lower())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return SPACE_RE.sub(" ", re.sub(r"[^a-z0-9]+", " ", text)).strip()


def slug_to_name(slug: str) -> str:
    return SPACE_RE.sub(" ", re.sub(r"[-_.]+", " ", slug)).strip().title()


def valid_slug(slug: str | None) -> bool:
    if not slug or len(slug) > 100 or slug.lower() in RESERVED_SLUGS:
        return False
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", slug):
        return False
    return not slug.isdigit()


def extract_board_token(provider: str, url: str) -> str | None:
    """Extract a tenant token only from a configured ATS host."""
    if provider not in PROVIDERS:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    for domain, mode in PROVIDERS[provider]["hosts"]:
        if mode == "path" and host == domain:
            parts = [part for part in parsed.path.split("/") if part]
            if not parts:
                continue
            slug = parts[0]
            if provider == "workable" and slug.lower() == "j":
                continue
            if provider == "rippling" and (len(parts) < 2 or parts[1].lower() != "jobs"):
                continue
            if valid_slug(slug):
                return slug.lower()
        if mode == "workday" and host.endswith("." + domain):
            match = re.fullmatch(r"([a-z0-9-]+)\.(wd[0-9]+)\.myworkdayjobs\.com", host)
            if not match:
                continue
            parts = [part for part in parsed.path.split("/") if part]
            if not parts or parts[0].lower() in {"wday", "api", "login"}:
                continue
            if re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[0]):
                locale, site = parts[0], parts[1] if len(parts) > 1 else ""
            else:
                locale, site = "en-US", parts[0]
            if not site or site.lower() in {"wday", "api", "login"}:
                continue
            return "|".join((match.group(1), match.group(2), site, locale))
        if mode == "subdomain" and host.endswith("." + domain):
            label = host[: -(len(domain) + 1)]
            if "." not in label and valid_slug(label):
                return label.lower()
    return None


class HostRateLimiter:
    """Enforce a minimum interval between request starts on each API host."""

    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}

    async def wait(self, host: str, interval: float) -> None:
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            delay = interval - (now - self._last.get(host, 0.0))
            if delay > 0:
                await asyncio.sleep(delay)
            self._last[host] = time.monotonic()


async def request_with_retries(
    client: httpx.AsyncClient,
    url: str,
    *,
    limiter: HostRateLimiter | None = None,
    interval: float = 0.1,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    retries: int = 3,
) -> tuple[httpx.Response | None, str | None]:
    host = urlparse(url).netloc.lower()
    for attempt in range(retries):
        if limiter:
            await limiter.wait(host, interval)
        try:
            response = await client.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            if attempt + 1 == retries:
                return None, f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(2 ** attempt)
            continue
        if response.status_code in (404, 410):
            return response, f"HTTP {response.status_code}"
        if response.status_code in (403, 429, 500, 502, 503, 504):
            if attempt + 1 == retries:
                return response, f"HTTP {response.status_code} after retries"
            retry_after = response.headers.get("Retry-After")
            try:
                wait = float(retry_after) if retry_after else min(2 ** (attempt + 1), 30)
            except ValueError:
                wait = min(2 ** (attempt + 1), 30)
            await asyncio.sleep(max(1.0, min(wait, 60.0)))
            continue
        if response.status_code >= 400:
            return response, f"HTTP {response.status_code}"
        return response, None
    return None, "request failed"


def _simple_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return ", ".join(part for part in (_simple_text(item) for item in value) if part)
    if isinstance(value, dict):
        fields = (
            "name", "label", "city", "state", "region", "country", "country_code",
            "countryCode", "country_name", "countryName", "fullLocation",
            "displayName", "addressLocality", "addressCountry",
        )
        return ", ".join(part for part in (_simple_text(value.get(key)) for key in fields) if part)
    return ""


def job_location_text(provider: str, job: dict[str, Any]) -> str:
    values: list[Any] = []
    if job.get("_parsed_location"):
        values.append(job["_parsed_location"])
    for key in (
        "location", "locations", "allLocations", "office", "offices",
        "workLocation", "workLocations", "jobLocation", "address",
        "city", "state", "region", "country", "country_code",
        "countryCode", "country_name", "countryName",
    ):
        if job.get(key) is not None:
            values.append(job.get(key))
    categories = job.get("categories")
    if isinstance(categories, dict):
        for key in ("location", "allLocations", "country", "city"):
            if categories.get(key) is not None:
                values.append(categories.get(key))
    return ", ".join(part for part in (_simple_text(v) for v in values) if part)


def is_france_location(location: Any, country_hint: Any = None) -> bool:
    """Require France/FR or an explicitly named French city."""
    if isinstance(country_hint, dict):
        country_hint = country_hint.get("code") or country_hint.get("name")
    country = norm_text(country_hint)
    if country in {"fr", "france", "french republic"}:
        return True
    text = norm_text(_simple_text(location))
    if re.search(r"(^| )france($| )", text) or re.search(r"(^| )fr($| )", text):
        return True
    padded = f" {text} "
    return any(f" {norm_text(city)} " in padded for city in FR_CITIES)


def job_is_in_france(provider: str, job: dict[str, Any]) -> bool:
    country_hint = (
        job.get("country_code") or job.get("countryCode") or job.get("country")
        or job.get("country_name") or job.get("countryName")
    )
    return is_france_location(job_location_text(provider, job), country_hint)


def _job_rows(provider: str, payload: Any, slug: str | None = None) -> list[dict[str, Any]]:
    if provider == "lever":
        return payload if isinstance(payload, list) else []
    if provider == "breezy":
        if isinstance(payload, list):
            return payload
        return payload.get("jobs", payload.get("positions", [])) if isinstance(payload, dict) else []
    if provider == "recruitee":
        return payload.get("offers", []) if isinstance(payload, dict) else []
    if provider in {"greenhouse", "ashby", "workable"}:
        key = "jobs"
        return payload.get(key, []) if isinstance(payload, dict) else []
    if provider == "teamtailor":
        feed = feedparser.parse(payload)
        rows = []
        for entry in feed.entries:
            row = dict(entry)
            location_values = [
                _simple_text(value)
                for key, value in row.items()
                if any(term in key.lower() for term in ("location", "country", "city"))
            ]
            row["_parsed_location"] = ", ".join(value for value in location_values if value)
            rows.append(row)
        return rows
    if provider == "gem":
        if isinstance(payload, list):
            return payload
        return payload.get("job_posts", payload.get("jobs", payload.get("data", []))) if isinstance(payload, dict) else []
    if provider == "pinpoint":
        if isinstance(payload, list):
            return payload
        return payload.get("postings", payload.get("jobs", payload.get("data", []))) if isinstance(payload, dict) else []
    if provider == "bamboohr":
        if isinstance(payload, list):
            return payload
        return payload.get("result", payload.get("jobs", payload.get("positions", []))) if isinstance(payload, dict) else []
    if provider == "workday":
        if not isinstance(payload, dict):
            return []
        rows = []
        try:
            tenant, region, site, locale = (slug or "").split("|", 3)
        except ValueError:
            return []
        host = f"{tenant}.{region}.myworkdayjobs.com"
        for item in payload.get("jobPostings", []):
            if not isinstance(item, dict):
                continue
            external_path = str(item.get("externalPath") or "")
            if not external_path.startswith("/") or external_path.startswith("//"):
                continue
            info = item.get("jobPostingInfo") if isinstance(item.get("jobPostingInfo"), dict) else {}
            bullets = item.get("bulletFields") if isinstance(item.get("bulletFields"), list) else []
            rows.append({
                "id": info.get("jobReqId") or info.get("jobPostingId") or (bullets[0] if bullets else external_path),
                "title": item.get("title") or info.get("title") or "",
                "url": f"https://{host}/{locale}/{site}{external_path}",
                "location": item.get("locationsText") or info.get("location") or "",
                "department": info.get("jobFamilyGroup") or info.get("jobFamily"),
                "startDate": info.get("startDate"),
                "description": info.get("jobDescription") or info.get("externalJobDescription") or "",
            })
        return rows
    if provider == "rippling":
        if isinstance(payload, list):
            return payload
        return payload.get("items", payload.get("jobs", payload.get("results", payload.get("data", [])))) if isinstance(payload, dict) else []
    return []


def _external_id(job: dict[str, Any]) -> str:
    value = (
        job.get("id") or job.get("jobId") or job.get("job_id") or job.get("shortcode")
        or job.get("code") or job.get("guid") or job.get("jobPostingId")
        or job.get("uuid") or job.get("url") or job.get("link")
    )
    return str(value).strip() if value is not None else ""


def _apply_url(provider: str, slug: str, job: dict[str, Any]) -> str:
    value = (
        job.get("absolute_url") or job.get("hostedUrl") or job.get("applyUrl")
        or job.get("application_url") or job.get("careers_url")
        or job.get("url") or job.get("link") or job.get("jobUrl")
    )
    if not value and provider == "recruitee":
        value = f"https://{slug}.recruitee.com/o/{_external_id(job)}"
    if not value and provider == "breezy":
        value = f"https://{slug}.breezy.hr/p/{_external_id(job)}"
    if not value and provider == "bamboohr":
        value = f"https://{slug}.bamboohr.com/careers/{_external_id(job)}"
    if not value and provider == "pinpoint":
        value = f"https://{slug}.pinpointhq.com/postings/{_external_id(job)}"
    if not value and provider == "rippling":
        value = f"https://ats.rippling.com/{slug}/jobs/{_external_id(job)}"
    if not value:
        return ""
    value = str(value).strip()
    if value.startswith("/"):
        host = {
            "breezy": f"{slug}.breezy.hr",
            "pinpoint": f"{slug}.pinpointhq.com",
            "bamboohr": f"{slug}.bamboohr.com",
            "rippling": f"ats.rippling.com/{slug}",
        }.get(provider)
        if host:
            value = f"https://{host}{value}"
    return value if value.startswith("https://") else ""


def _posted_at(job: dict[str, Any]) -> str | None:
    value = (
        job.get("first_published") or job.get("published_at") or job.get("published_on")
        or job.get("published_date") or job.get("publishedAt") or job.get("createdAt")
        or job.get("created_at") or job.get("startDate") or job.get("pubdate")
        or job.get("published")
    )
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            stamp = value / 1000 if value > 10_000_000_000 else value
            return datetime.fromtimestamp(stamp, timezone.utc).date().isoformat()
        except (ValueError, OverflowError, OSError):
            return None
    value = str(value).strip()
    return value[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", value) else None


def clean_public_text(value: Any, limit: int = 6000) -> str:
    """Remove markup and contact details before storing public vacancy text."""
    text = html.unescape(str(value or ""))
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    text = HTML_RE.sub(" ", text)
    text = EMAIL_RE.sub("[email removed]", text)
    text = PHONE_RE.sub("[phone removed]", text)
    return SPACE_RE.sub(" ", text).strip()[:limit]


def normalise_job(provider: str, slug: str, job: dict[str, Any]) -> dict[str, Any] | None:
    title = str(job.get("title") or job.get("text") or job.get("name") or "").strip()
    external_id = _external_id(job)
    apply_url = _apply_url(provider, slug, job)
    if not title or not external_id or not apply_url:
        return None
    location = job.get("_parsed_location") or job_location_text(provider, job)
    description_value = (
        job.get("content") or job.get("descriptionPlain") or job.get("description")
        or job.get("description_html") or job.get("description_text") or job.get("summary")
    )
    description = clean_public_text(description_value)
    department = job.get("department") or job.get("team")
    if isinstance(department, dict):
        department = _simple_text(department)
    if not department and isinstance(job.get("categories"), dict):
        department = job["categories"].get("team") or job["categories"].get("department")
    company_value = job.get("company_name") or job.get("companyName") or job.get("company")
    company_name = _simple_text(company_value) if company_value else ""
    workplace = str(job.get("workplaceType") or job.get("workplace") or job.get("workplace_type") or "").lower()
    remote = bool(REMOTE_RE.search(location) or "remote" in workplace or job.get("remote") is True or job.get("telecommuting") is True)
    return {
        "external_id": external_id,
        "title": title,
        "apply_url": apply_url,
        "location": location,
        "city": job.get("city"),
        "department": str(department).strip() if department else None,
        "posted_at": _posted_at(job),
        "description": description,
        "company_name": company_name,
        "remote": remote,
    }


def _workday_parts(slug: str) -> tuple[str, str, str, str]:
    try:
        tenant, region, site, locale = slug.split("|", 3)
    except ValueError as exc:
        raise ValueError("invalid Workday board identifier") from exc
    if not re.fullmatch(r"[a-z0-9-]+", tenant) or not re.fullmatch(r"wd[0-9]+", region):
        raise ValueError("invalid Workday tenant")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", site) or not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", locale):
        raise ValueError("invalid Workday site or locale")
    return tenant, region, site, locale


def _workday_board_url(slug: str) -> str:
    tenant, region, site, locale = _workday_parts(slug)
    return f"https://{tenant}.{region}.myworkdayjobs.com/{locale}/{site}"


async def _workday_request(
    client: httpx.AsyncClient,
    limiter: HostRateLimiter,
    url: str,
    payload: dict[str, Any],
    interval: float,
) -> tuple[httpx.Response | None, str | None]:
    host = urlparse(url).netloc.lower()
    for attempt in range(3):
        await limiter.wait(host, interval)
        try:
            response = await client.post(
                url,
                json=payload,
                follow_redirects=False,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            if attempt == 2:
                return None, f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(2 ** attempt)
            continue
        if response.status_code in (301, 302, 303, 307, 308):
            return response, "Workday endpoint redirected; not following POST redirect"
        if response.status_code in (403, 429, 500, 502, 503, 504):
            if attempt == 2:
                return response, f"HTTP {response.status_code} after retries"
            retry_after = response.headers.get("Retry-After")
            try:
                wait = float(retry_after) if retry_after else min(2 ** (attempt + 1), 30)
            except ValueError:
                wait = min(2 ** (attempt + 1), 30)
            await asyncio.sleep(max(1.0, min(wait, 60.0)))
            continue
        if response.status_code >= 400:
            return response, f"HTTP {response.status_code}"
        return response, None
    return None, "Workday request failed"


async def _fetch_workday_jobs(
    slug: str,
    client: httpx.AsyncClient,
    limiter: HostRateLimiter,
    interval: float,
) -> tuple[bool, list[dict[str, Any]], str | None]:
    try:
        tenant, region, site, locale = _workday_parts(slug)
    except ValueError as exc:
        return False, [], str(exc)
    host = f"{tenant}.{region}.myworkdayjobs.com"
    base = f"https://{host}"
    endpoint = f"{base}/wday/cxs/{tenant}/{site}/jobs"

    # Workday's search API is an undocumented public careers-site endpoint. Only
    # read it when the specific tenant's robots.txt allows this exact URL.
    robots_url = f"{base}/robots.txt"
    robots_response, robots_error = await request_with_retries(
        client, robots_url, limiter=limiter, interval=interval, retries=2
    )
    if robots_error and not (robots_response is not None and robots_response.status_code in (404, 410)):
        return False, [], f"could not verify Workday robots.txt: {robots_error}"
    if robots_response is not None and robots_response.status_code not in (404, 410):
        parser = RobotFileParser()
        parser.set_url(robots_url)
        parser.parse(robots_response.text.splitlines())
        user_agent = client.headers.get("User-Agent", "*")
        if not parser.can_fetch(user_agent, endpoint):
            return False, [], "robots.txt disallows Workday public jobs endpoint"

    out: list[dict[str, Any]] = []
    offset, total = 0, None
    page_size, max_postings = 20, 2000
    while offset < max_postings:
        payload, error = await _workday_request(
            client, limiter, endpoint,
            {"appliedFacets": {}, "limit": page_size, "offset": offset, "searchText": ""},
            interval,
        )
        if error or payload is None:
            return False, [], error or "Workday returned no response"
        try:
            data = payload.json()
        except ValueError:
            return False, [], "invalid Workday JSON response"
        if not payload_shape_valid("workday", data):
            return False, [], "unexpected Workday response shape"
        if total is None:
            try:
                total = int(data.get("total", len(data["jobPostings"])))
            except (TypeError, ValueError):
                total = len(data["jobPostings"])
        out.extend(_job_rows("workday", data, slug))
        if len(data["jobPostings"]) < page_size:
            break
        offset += len(data["jobPostings"])
        if offset >= total:
            break
    return True, out, None


def board_url(provider: str, slug: str) -> str:
    urls = {
        "greenhouse": f"https://boards.greenhouse.io/{slug}",
        "lever": f"https://jobs.lever.co/{slug}",
        "ashby": f"https://jobs.ashbyhq.com/{slug}",
        "workable": f"https://apply.workable.com/{slug}",
        "breezy": f"https://{slug}.breezy.hr/",
        "recruitee": f"https://{slug}.recruitee.com/",
        "teamtailor": f"https://{slug}.teamtailor.com/jobs",
        "gem": f"https://jobs.gem.com/{slug}",
        "pinpoint": f"https://{slug}.pinpointhq.com/",
        "bamboohr": f"https://{slug}.bamboohr.com/careers/list",
        "rippling": f"https://ats.rippling.com/{slug}/jobs",
    }
    if provider == "workday":
        return _workday_board_url(slug)
    if provider not in urls:
        raise ValueError(f"Unsupported provider: {provider}")
    return urls[provider]


def board_request(provider: str, slug: str) -> tuple[str, dict[str, Any] | None]:
    if provider == "workday":
        raise ValueError("Workday uses its public career-site POST endpoint")
    if provider == "greenhouse":
        return f"https://boards-api.greenhouse.io/v1/boards/{quote(slug, safe='')}/jobs", {"content": "true"}
    if provider == "lever":
        return f"https://api.lever.co/v0/postings/{quote(slug, safe='')}?mode=json", None
    if provider == "ashby":
        return f"https://api.ashbyhq.com/posting-api/job-board/{quote(slug, safe='')}", {"includeCompensation": "true"}
    if provider == "workable":
        return f"https://apply.workable.com/api/v1/widget/accounts/{quote(slug, safe='')}?details=true", None
    if provider == "breezy":
        return f"https://{slug}.breezy.hr/json", None
    if provider == "recruitee":
        return f"https://{slug}.recruitee.com/api/offers/", None
    if provider == "teamtailor":
        return f"https://{slug}.teamtailor.com/jobs.rss", None
    if provider == "gem":
        return f"https://api.gem.com/job_board/v0/{quote(slug, safe='')}/job_posts/", None
    if provider == "pinpoint":
        return f"https://{slug}.pinpointhq.com/postings.json", None
    if provider == "bamboohr":
        return f"https://{slug}.bamboohr.com/careers/list", None
    if provider == "rippling":
        return f"https://ats.rippling.com/api/v2/board/{quote(slug, safe='')}/jobs", {"page": "0"}
    raise ValueError(f"Unsupported provider: {provider}")


def payload_shape_valid(provider: str, payload: Any) -> bool:
    """Reject unknown response formats instead of treating them as empty boards."""
    if provider == "teamtailor":
        parsed = feedparser.parse(payload)
        # A valid empty RSS feed still has a recognized feed version.
        return bool(parsed.version)
    if provider == "lever":
        return isinstance(payload, list)
    if provider == "breezy":
        return isinstance(payload, list) or (
            isinstance(payload, dict)
            and any(isinstance(payload.get(key), list) for key in ("jobs", "positions"))
        )
    expected_arrays = {
        "greenhouse": ("jobs",),
        "ashby": ("jobs",),
        "workable": ("jobs",),
        "recruitee": ("offers",),
        "gem": ("job_posts", "jobs", "data"),
        "pinpoint": ("postings", "jobs", "data"),
        "bamboohr": ("result", "jobs", "positions"),
        "rippling": ("items", "jobs", "results", "data"),
        "workday": ("jobPostings",),
    }
    keys = expected_arrays.get(provider, ())
    return isinstance(payload, dict) and any(
        isinstance(payload.get(key), list) for key in keys
    )


async def fetch_board_jobs(
    provider: str,
    slug: str,
    client: httpx.AsyncClient,
    limiter: HostRateLimiter,
) -> tuple[bool, list[dict[str, Any]], str | None]:
    """Return success, France jobs and an error. An empty successful feed is valid."""
    if provider == "recruitee" and date.today() >= RECRUITEE_TOKEN_DATE:
        return False, [], "disabled: Recruitee requires a Careers API token from 2027-02-10"
    if provider == "workday":
        success, rows, error = await _fetch_workday_jobs(
            slug, client, limiter, float(PROVIDERS[provider]["interval"])
        )
        if not success:
            return False, [], error
    else:
        try:
            url, params = board_request(provider, slug)
        except ValueError as exc:
            return False, [], str(exc)
        headers = {"Accept": "application/json, application/rss+xml, application/xml"}
        interval = float(PROVIDERS[provider]["interval"])
        response, error = await request_with_retries(
            client, url, limiter=limiter, interval=interval, params=params, headers=headers
        )
        if response is None or error:
            return False, [], error or "no response"
        try:
            payload = response.content if provider == "teamtailor" else response.json()
        except ValueError as exc:
            return False, [], f"invalid public feed: {exc}"
        if not payload_shape_valid(provider, payload):
            return False, [], "unexpected public feed response shape"
        rows = _job_rows(provider, payload, slug)
    if provider == "rippling" and isinstance(payload, dict):
        try:
            total_pages = min(int(payload.get("totalPages", 1)), 10)
        except (TypeError, ValueError):
            total_pages = 1
        for page in range(1, total_pages):
            page_response, page_error = await request_with_retries(
                client, url, limiter=limiter, interval=interval,
                params={"page": str(page)}, headers=headers
            )
            if page_error or page_response is None:
                break
            try:
                page_payload = page_response.json()
            except ValueError:
                return False, [], "invalid Rippling page response"
            if not payload_shape_valid(provider, page_payload):
                return False, [], "unexpected Rippling page response shape"
            rows.extend(_job_rows(provider, page_payload, slug))

    france_jobs = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if provider == "recruitee" and row.get("status") not in (None, "published"):
            continue
        if not job_is_in_france(provider, row):
            continue
        normalized = normalise_job(provider, slug, row)
        if normalized:
            france_jobs.append(normalized)
    return True, france_jobs, None


def create_database(path: str | Path) -> sqlite3.Connection:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS companies (
            company_id INTEGER PRIMARY KEY AUTOINCREMENT,
            siren TEXT UNIQUE,
            identity_key TEXT UNIQUE,
            name TEXT NOT NULL,
            legal_name TEXT,
            naf_code TEXT,
            city TEXT,
            etat_administratif TEXT,
            nature_juridique TEXT,
            statut_diffusion TEXT,
            registry_match TEXT NOT NULL DEFAULT 'not_checked',
            registry_checked_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ats_sources (
            source_id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL REFERENCES companies(company_id),
            display_name TEXT NOT NULL,
            ats_type TEXT NOT NULL,
            ats_slug TEXT NOT NULL,
            board_url TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            first_discovered_at TEXT NOT NULL,
            last_checked_at TEXT,
            last_success_at TEXT,
            last_error TEXT,
            UNIQUE(ats_type, ats_slug)
        );
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            source_id INTEGER NOT NULL REFERENCES ats_sources(source_id),
            external_id TEXT NOT NULL,
            title TEXT NOT NULL,
            department TEXT,
            location TEXT,
            city TEXT,
            apply_url TEXT NOT NULL,
            posted_at TEXT,
            description TEXT,
            remote INTEGER NOT NULL DEFAULT 0,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            UNIQUE(source_id, external_id)
        );
        CREATE INDEX IF NOT EXISTS idx_sources_active ON ats_sources(active);
        CREATE INDEX IF NOT EXISTS idx_jobs_active ON jobs(is_active, posted_at);
        CREATE INDEX IF NOT EXISTS idx_jobs_source ON jobs(source_id, is_active);
        CREATE INDEX IF NOT EXISTS idx_companies_siren ON companies(siren);
        """
    )
    connection.commit()
    return connection


def upsert_company(
    connection: sqlite3.Connection,
    identity_key: str,
    display_name: str,
    registry: dict[str, Any] | None,
    registry_match: str,
    checked_at: str,
) -> int:
    registry = registry or {}
    siren = registry.get("siren")
    row = None
    if siren:
        row = connection.execute("SELECT company_id FROM companies WHERE siren=?", (siren,)).fetchone()
    if row is None:
        row = connection.execute("SELECT company_id FROM companies WHERE identity_key=?", (identity_key,)).fetchone()
    siege = registry.get("siege") or {}
    legal_name = registry.get("nom_raison_sociale") or registry.get("nom_complet")
    naf = siege.get("activite_principale") or registry.get("activite_principale")
    city = siege.get("libelle_commune") or siege.get("commune")

    if row:
        company_id = int(row["company_id"])
        connection.execute(
            """
            UPDATE companies SET
                siren=COALESCE(?, siren),
                identity_key=COALESCE(identity_key, ?),
                legal_name=COALESCE(?, legal_name),
                naf_code=COALESCE(?, naf_code),
                city=COALESCE(?, city),
                etat_administratif=COALESCE(?, etat_administratif),
                nature_juridique=COALESCE(?, nature_juridique),
                statut_diffusion=COALESCE(?, statut_diffusion),
                registry_match=CASE WHEN ?='exact' THEN 'exact' ELSE registry_match END,
                registry_checked_at=?,
                updated_at=?
            WHERE company_id=?
            """,
            (
                siren, identity_key, legal_name, naf, city,
                registry.get("etat_administratif"), registry.get("nature_juridique"),
                registry.get("statut_diffusion"), registry_match,
                checked_at, checked_at, company_id,
            ),
        )
        return company_id

    cursor = connection.execute(
        """
        INSERT INTO companies (
            siren, identity_key, name, legal_name, naf_code, city,
            etat_administratif, nature_juridique, statut_diffusion,
            registry_match, registry_checked_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            siren, identity_key, display_name, legal_name, naf, city,
            registry.get("etat_administratif"), registry.get("nature_juridique"),
            registry.get("statut_diffusion"), registry_match,
            checked_at, checked_at, checked_at,
        ),
    )
    return int(cursor.lastrowid)


def upsert_source(
    connection: sqlite3.Connection,
    company_id: int,
    display_name: str,
    provider: str,
    slug: str,
    checked_at: str,
) -> int:
    connection.execute(
        """
        INSERT INTO ats_sources (
            company_id, display_name, ats_type, ats_slug, board_url,
            active, first_discovered_at, last_checked_at, last_success_at, last_error
        ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, NULL)
        ON CONFLICT(ats_type, ats_slug) DO UPDATE SET
            company_id=excluded.company_id,
            display_name=excluded.display_name,
            board_url=excluded.board_url,
            active=1,
            last_checked_at=excluded.last_checked_at,
            last_success_at=excluded.last_success_at,
            last_error=NULL
        """,
        (company_id, display_name, provider, slug, board_url(provider, slug),
         checked_at, checked_at, checked_at),
    )
    row = connection.execute(
        "SELECT source_id FROM ats_sources WHERE ats_type=? AND ats_slug=?",
        (provider, slug),
    ).fetchone()
    return int(row["source_id"])


def record_source_check(
    connection: sqlite3.Connection,
    source_id: int,
    checked_at: str,
    success: bool,
    error: str | None = None,
) -> None:
    if success:
        connection.execute(
            "UPDATE ats_sources SET last_checked_at=?, last_success_at=?, last_error=NULL WHERE source_id=?",
            (checked_at, checked_at, source_id),
        )
    else:
        connection.execute(
            "UPDATE ats_sources SET last_checked_at=?, last_error=? WHERE source_id=?",
            (checked_at, (error or "unknown error")[:500], source_id),
        )


def upsert_jobs_for_source(
    connection: sqlite3.Connection,
    source_id: int,
    jobs: list[dict[str, Any]],
    checked_at: str,
) -> int:
    """Upsert a successful feed and expire only missing jobs from that board."""
    count = 0
    for job in jobs:
        external_id = str(job["external_id"])
        job_id = f"{source_id}:{external_id}"
        connection.execute(
            """
            INSERT INTO jobs (
                job_id, source_id, external_id, title, department, location,
                city, apply_url, posted_at, description, remote,
                first_seen_at, last_seen_at, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(job_id) DO UPDATE SET
                title=excluded.title,
                department=excluded.department,
                location=excluded.location,
                city=excluded.city,
                apply_url=excluded.apply_url,
                posted_at=COALESCE(excluded.posted_at, jobs.posted_at),
                description=excluded.description,
                remote=excluded.remote,
                last_seen_at=excluded.last_seen_at,
                is_active=1
            """,
            (
                job_id, source_id, external_id, job["title"], job.get("department"),
                job.get("location"), job.get("city"), job["apply_url"],
                job.get("posted_at"), clean_public_text(job.get("description")),
                int(bool(job.get("remote"))), checked_at, checked_at,
            ),
        )
        count += 1
    connection.execute(
        "UPDATE jobs SET is_active=0 WHERE source_id=? AND last_seen_at<>? AND is_active=1",
        (source_id, checked_at),
    )
    return count


def classify_role(title: str, department: str | None = None) -> tuple[str, str]:
    combined = f"{title} {department or ''}"
    role = "other"
    for name, pattern in ROLE_RULES:
        if re.search(pattern, combined, re.I):
            role = name
            break
    level = "mid"
    for name, pattern in LEVEL_RULES:
        if re.search(pattern, title, re.I):
            level = name
            break
    return role, level


def find_salary(text: str) -> dict[str, Any] | None:
    match = re.search(
        r"(?:€|EUR)\s?(\d{2,3}(?:[.,\s]\d{3})+|\d{2,3}\s?[kK])"
        r"\s*(?:-|–|—|to|à)\s*(?:€|EUR)?\s?"
        r"(\d{2,3}(?:[.,\s]\d{3})+|\d{2,3}\s?[kK])",
        text or "", re.I,
    )
    if not match:
        return None
    def number(raw: str) -> int:
        raw = raw.lower().replace(" ", "").replace(",", "").replace(".", "")
        return int(raw[:-1]) * 1000 if raw.endswith("k") else int(raw)
    try:
        low, high = number(match.group(1)), number(match.group(2))
    except ValueError:
        return None
    return {"min": low, "max": high, "currency": "EUR"} if 8000 <= low <= high <= 1_000_000 else None


def language_hint(text: str) -> str:
    words = norm_text(text).split()
    french = sum(word in {"vous", "nous", "votre", "pour", "avec", "les", "des", "une", "dans"} for word in words)
    english = sum(word in {"the", "and", "you", "with", "for", "our", "your", "this", "are", "will"} for word in words)
    return "fr" if french > english and french >= 3 else "en"


def make_frontend_job(row: sqlite3.Row) -> dict[str, Any]:
    from crawler.geo import locate, locate_all

    location = row["location"] or "France"
    place = locate(location, "FR") or locate("France", "FR")
    locations = locate_all(location, "FR") or ([place] if place else [])
    role, level = classify_role(row["title"], row["department"])
    city = row["city"] or (place or {}).get("city") or "France"
    return {
        "id": row["job_id"],
        "company": row["display_name"],
        "title": row["title"],
        "url": row["apply_url"],
        "location": f"{city}, France" if city != "France" else location,
        "city": city,
        "country": "FR",
        "locations": locations,
        "lat": (place or {}).get("lat"),
        "lon": (place or {}).get("lon"),
        "remote": bool(row["remote"]),
        "role": role,
        "level": level,
        "language": language_hint(row["description"] or row["title"]),
        "salary": find_salary(row["description"] or ""),
        "department": row["department"],
        "posted": row["posted_at"],
        "snippet": clean_public_text(row["description"], 500),
        "first_seen": row["first_seen_at"],
        "last_seen": row["last_seen_at"],
        "career_source_verified": True,
        "source_last_checked": row["source_last_checked"],
        "company_registry_verified": bool(row["siren"]),
        "company_registry_checked_at": row["registry_checked_at"],
        "naf_code": row["naf_code"],
        "siren": row["siren"],
        "ats": row["ats_type"],
    }


def export_active_jobs(connection: sqlite3.Connection, output_dir: str | Path) -> int:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    rows = connection.execute(
        """
        SELECT j.*, s.display_name, s.ats_type, s.last_success_at AS source_last_checked,
               c.siren, c.naf_code, c.registry_checked_at
        FROM jobs j
        JOIN ats_sources s ON s.source_id=j.source_id
        JOIN companies c ON c.company_id=s.company_id
        WHERE j.is_active=1 AND s.active=1
        ORDER BY COALESCE(j.posted_at, j.first_seen_at) DESC
        """
    ).fetchall()
    payload = {
        "generated_at": utc_now(),
        "count": len(rows),
        "demo": False,
        "jobs": [make_frontend_job(row) for row in rows],
    }
    (output / "active_jobs.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    fields = [
        "id", "siren", "company", "naf_code", "ats", "title", "department",
        "location", "city", "country", "remote", "salary_min", "salary_max",
        "salary_currency", "posted", "apply_url", "first_seen", "last_seen",
    ]
    with (output / "active_jobs.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for job in payload["jobs"]:
            salary = job.get("salary") or {}
            writer.writerow({
                "id": job["id"], "siren": job.get("siren"), "company": job["company"],
                "naf_code": job.get("naf_code"), "ats": job["ats"], "title": job["title"],
                "department": job.get("department"), "location": job["location"],
                "city": job["city"], "country": job["country"], "remote": job["remote"],
                "salary_min": salary.get("min"), "salary_max": salary.get("max"),
                "salary_currency": salary.get("currency"), "posted": job.get("posted"),
                "apply_url": job["url"], "first_seen": job["first_seen"], "last_seen": job["last_seen"],
            })
    site_data = Path("site/data")
    site_data.mkdir(parents=True, exist_ok=True)
    (site_data / "jobs.json").write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    return len(rows)


def read_active_sources(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT s.*, c.name AS company_name, c.siren, c.naf_code, c.registry_checked_at
        FROM ats_sources s
        JOIN companies c ON c.company_id=s.company_id
        WHERE s.active=1
        ORDER BY s.ats_type, s.ats_slug
        """
    ).fetchall()
