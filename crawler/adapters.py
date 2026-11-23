"""Adapters for public employer career boards and applicant-tracking systems.

Each adapter has the signature  fetch(slug, http, **opts) -> list[dict]  and returns
"raw" jobs with these keys (all optional except id, title, url). Workday's public
career-site search endpoint is undocumented and may change; it is only used for
boards discovered from the employer's verified official careers site.

    id, title, url, location, country (ISO2 hint), remote (bool|None),
    department, posted (ISO date str), description (plain text, will be truncated)

All endpoints below are the public job-board feeds that the ATS vendors publish
for exactly this purpose (embedding jobs on career sites).
"""
from __future__ import annotations

import html
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urljoin

from .http import FetchError

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def strip_html(s: str | None) -> str:
    if not s:
        return ""
    s = html.unescape(s)  # Greenhouse double-escapes its content
    s = re.sub(r"</(p|div|li|h[1-6]|br)>|<br\s*/?>", " ", s, flags=re.I)
    s = TAG_RE.sub(" ", s)
    return WS_RE.sub(" ", html.unescape(s)).strip()


def _iso_from_ms(ms) -> str | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _date(s) -> str | None:
    if not s:
        return None
    return str(s)[:10]


# ---------------------------------------------------------------- Greenhouse
def greenhouse(slug, http, **_):
    data = http.get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    out = []
    for j in data.get("jobs", []):
        out.append({
            "id": str(j["id"]),
            "title": j.get("title", ""),
            "url": j.get("absolute_url", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "department": ((j.get("departments") or [{}])[0] or {}).get("name"),
            "posted": _date(j.get("first_published") or j.get("updated_at")),
            "description": strip_html(j.get("content")),
        })
    return out


# --------------------------------------------------------------------- Lever
def lever(slug, http, eu=False, **_):
    host = "api.eu.lever.co" if eu else "api.lever.co"
    data = http.get_json(f"https://{host}/v0/postings/{slug}?mode=json")
    if not isinstance(data, list):
        raise FetchError(f"unexpected Lever payload for {slug}")
    out = []
    for j in data:
        cats = j.get("categories") or {}
        loc = cats.get("location") or ", ".join(cats.get("allLocations") or [])
        wt = (j.get("workplaceType") or "").lower()
        out.append({
            "id": j["id"],
            "title": j.get("text", ""),
            "url": j.get("hostedUrl") or j.get("applyUrl", ""),
            "location": loc,
            "remote": True if wt == "remote" else None,
            "department": cats.get("team") or cats.get("department"),
            "posted": _iso_from_ms(j.get("createdAt")),
            "description": j.get("descriptionPlain") or strip_html(j.get("description")),
        })
    return out


# --------------------------------------------------------------------- Ashby
def ashby(slug, http, **_):
    data = http.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true")
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        addr = ((j.get("address") or {}).get("postalAddress")) or {}
        loc = j.get("location") or ""
        if addr.get("addressLocality") and addr["addressLocality"] not in loc:
            loc = f"{loc} {addr['addressLocality']}".strip()
        comp = (j.get("compensation") or {}).get("compensationTierSummary") or ""
        out.append({
            "id": j.get("id") or j.get("jobUrl"),
            "title": j.get("title", ""),
            "url": j.get("applyUrl") or j.get("jobUrl", ""),
            "location": loc,
            "country_text": addr.get("addressCountry"),
            # Ashby sets isRemote for both Remote and Hybrid. Prefer the explicit
            # workplaceType so hybrid jobs are not mislabeled as fully remote.
            "remote": True if (j.get("workplaceType") or "").lower() == "remote" else (True if j.get("isRemote") and not j.get("workplaceType") else None),
            "department": j.get("department") or j.get("team"),
            "posted": _date(j.get("publishedAt")),
            "description": (comp + " " + (j.get("descriptionPlain") or "")).strip(),
        })
    return out


# ------------------------------------------------------------------ Personio
def personio(slug, http, **_):
    last = None
    text = None
    for domain in ("jobs.personio.de", "jobs.personio.com"):
        try:
            text = http.get_text(f"https://{slug}.{domain}/xml?language=en")
            base = f"https://{slug}.{domain}"
            break
        except FetchError as e:
            last = e
    if text is None:
        raise last or FetchError(f"personio feed not found for {slug}")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise FetchError(f"bad Personio XML for {slug}: {e}") from e
    out = []
    for p in root.iter("position"):
        pid = (p.findtext("id") or "").strip()
        if not pid:
            continue
        desc = " ".join(
            strip_html(d.findtext("value")) for d in p.iter("jobDescription")
        )
        out.append({
            "id": pid,
            "title": (p.findtext("name") or "").strip(),
            "url": f"{base}/job/{pid}",
            "location": (p.findtext("office") or "").strip(),
            "department": (p.findtext("department") or "").strip() or None,
            "posted": _date(p.findtext("createdAt")),
            "description": desc,
        })
    return out


# ----------------------------------------------------------------- Recruitee
def recruitee(slug, http, **_):
    data = http.get_json(f"https://{slug}.recruitee.com/api/offers/")
    out = []
    for j in data.get("offers", []):
        if j.get("status") not in (None, "published"):
            continue
        loc = ", ".join(x for x in (j.get("city"), j.get("country")) if x) or j.get("location", "")
        out.append({
            "id": str(j["id"]),
            "title": j.get("title", ""),
            "url": j.get("careers_url", ""),
            "location": loc,
            "country": j.get("country_code"),
            "remote": True if (j.get("remote") or j.get("workplace") == "remote") else None,
            "department": j.get("department"),
            "posted": _date(j.get("published_at") or j.get("created_at")),
            "description": strip_html(j.get("description")),
        })
    return out


# ------------------------------------------------------------------ Workable
def workable(slug, http, **_):
    data = http.get_json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true")
    out = []
    for j in data.get("jobs", []):
        loc = ", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x)
        out.append({
            "id": j.get("shortcode") or j.get("code"),
            "title": j.get("title", ""),
            "url": j.get("application_url") or j.get("url", ""),
            "location": loc,
            "remote": True if j.get("telecommuting") else None,
            "department": j.get("department"),
            "posted": _date(j.get("published_on") or j.get("created_at")),
            "description": strip_html(j.get("description")),
        })
    return out


# ------------------------------------------------------------ SmartRecruiters
def smartrecruiters(slug, http, **_):
    out, offset = [], 0
    while True:
        data = http.get_json(
            f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100&offset={offset}"
        )
        items = data.get("content", [])
        for j in items:
            loc = j.get("location") or {}
            out.append({
                "id": str(j["id"]),
                "title": j.get("name", ""),
                "url": f"https://jobs.smartrecruiters.com/{slug}/{j['id']}",
                "location": loc.get("fullLocation") or ", ".join(
                    x for x in (loc.get("city"), loc.get("country")) if x),
                "country": (loc.get("country") or "").upper() or None,
                "remote": True if loc.get("remote") else None,
                "department": (j.get("department") or {}).get("label"),
                "posted": _date(j.get("releasedDate")),
                "description": "",
            })
        offset += len(items)
        if not items or offset >= int(data.get("totalFound", 0)) or offset > 2000:
            break
    return out


# ---------------------------------------------------------------- Teamtailor
def teamtailor(slug, http, **_):
    """Teamtailor exposes a public RSS feed per career site. Parsing is tolerant
    because field names vary slightly between customers."""
    text = http.get_text(f"https://{slug}.teamtailor.com/jobs.rss")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise FetchError(f"bad Teamtailor RSS for {slug}: {e}") from e
    out = []
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        if not link:
            continue
        loc_parts = []
        for el in item.iter():
            tag = el.tag.split("}")[-1].lower()
            if tag in ("city", "country") and (el.text or "").strip():
                loc_parts.append(el.text.strip())
        remote = (item.findtext("{*}remoteStatus") or "").lower()
        out.append({
            "id": (item.findtext("guid") or link).strip(),
            "title": (item.findtext("title") or "").strip(),
            "url": link,
            "location": ", ".join(dict.fromkeys(loc_parts)),
            "remote": True if remote in ("fully", "remote") else None,
            "department": (item.findtext("{*}department") or "").strip() or None,
            "posted": _date(item.findtext("pubDate")) if (item.findtext("pubDate") or "")[:4].isdigit() else None,
            "description": strip_html(item.findtext("description")),
        })
    return out



# ------------------------------------------------------------------- Workday
WORKDAY_PAGE_SIZE = 20
WORKDAY_MAX_POSTINGS = 2000


def workday(slug, http, **_):
    """Read the public search endpoint used by the linked Workday career site.

    This is an undocumented front-end endpoint, not Workday's private HCM API.
    Only boards discovered from an approved employer career page are passed in.
    """
    try:
        tenant, region, site, locale = slug.split("|", 3)
    except ValueError as exc:
        raise FetchError("invalid Workday board identifier") from exc
    host = f"{tenant}.{region}.myworkdayjobs.com"
    base = f"https://{host}"
    endpoint = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    if not http.allowed_by_robots(endpoint):
        raise FetchError(f"robots.txt disallows Workday job feed: {endpoint}")
    out = []
    offset = 0
    total = None
    while offset < WORKDAY_MAX_POSTINGS:
        payload = http.post_json(endpoint, {
            "appliedFacets": {}, "limit": WORKDAY_PAGE_SIZE,
            "offset": offset, "searchText": "",
        }, headers={"Content-Type": "application/json", "Accept": "application/json"})
        postings = payload.get("jobPostings") or []
        if total is None:
            try:
                total = int(payload.get("total", len(postings)))
            except (TypeError, ValueError):
                total = len(postings)
        for item in postings:
            external_path = str(item.get("externalPath") or "")
            if not external_path.startswith("/") or external_path.startswith("//"):
                continue
            url = f"{base}/{locale}/{site}{external_path}"
            info = item.get("jobPostingInfo") if isinstance(item.get("jobPostingInfo"), dict) else {}
            bullet_fields = item.get("bulletFields") if isinstance(item.get("bulletFields"), list) else []
            out.append({
                "id": str(info.get("jobReqId") or info.get("jobPostingId") or (bullet_fields[0] if bullet_fields else external_path)),
                "title": item.get("title") or info.get("title") or "",
                "url": url,
                "location": item.get("locationsText") or info.get("location") or "",
                "department": info.get("jobFamilyGroup") or info.get("jobFamily"),
                "posted": _date(info.get("startDate")),
                "description": strip_html(info.get("jobDescription") or info.get("externalJobDescription") or ""),
            })
        if not postings or len(postings) < WORKDAY_PAGE_SIZE:
            break
        offset += len(postings)
        if total is not None and offset >= total:
            break
    return out

# ------------------------------------------------- Generic JSON-LD JobPosting
def jsonld(slug, http, urls=None, **_):
    """Extract schema.org JobPosting records from verified custom career pages.

    Supports @graph, top-level arrays and @type arrays. This is intentionally
    structured-data extraction, not arbitrary full-site scraping.
    """
    out = []
    seen = set()

    def posting_nodes(value):
        if isinstance(value, list):
            for item in value:
                yield from posting_nodes(item)
            return
        if not isinstance(value, dict):
            return
        raw_type = value.get("@type", [])
        types = {raw_type} if isinstance(raw_type, str) else set(raw_type or [])
        if "JobPosting" in types:
            yield value
        if value.get("@graph") is not None:
            yield from posting_nodes(value["@graph"])
        if value.get("mainEntity") is not None:
            yield from posting_nodes(value["mainEntity"])

    for page_url in urls or []:
        if not http.allowed_by_robots(page_url):
            raise FetchError(f"robots.txt disallows {page_url}")
        page = http.get_text(page_url)
        for match in re.finditer(
            r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>',
            page, re.S | re.I,
        ):
            try:
                blob = json.loads(html.unescape(match.group(1)).strip())
            except (TypeError, ValueError):
                continue
            for posting in posting_nodes(blob):
                location = posting.get("jobLocation")
                locations = location if isinstance(location, list) else [location]
                location_labels = []
                for loc in locations:
                    if not isinstance(loc, dict):
                        continue
                    address = loc.get("address") or {}
                    if isinstance(address, dict):
                        label = ", ".join(
                            part for part in (
                                address.get("addressLocality"),
                                address.get("addressRegion"),
                                address.get("addressCountry"),
                            ) if isinstance(part, str) and part.strip()
                        )
                        if label and label not in location_labels:
                            location_labels.append(label)
                raw_identifier = posting.get("identifier")
                if isinstance(raw_identifier, dict):
                    identifier = raw_identifier.get("value") or raw_identifier.get("name")
                else:
                    identifier = raw_identifier
                job_url = urljoin(page_url, str(posting.get("url") or page_url))
                job_id = str(identifier or job_url or posting.get("title") or "")
                if not job_id or job_id in seen:
                    continue
                seen.add(job_id)
                out.append({
                    "id": job_id,
                    "title": posting.get("title", ""),
                    "url": job_url,
                    "location": " | ".join(location_labels),
                    "remote": True if posting.get("jobLocationType") == "TELECOMMUTE" else None,
                    "posted": _date(posting.get("datePosted")),
                    "description": strip_html(posting.get("description")),
                })
    return out


ADAPTERS = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "personio": personio,
    "recruitee": recruitee,
    "workable": workable,
    "smartrecruiters": smartrecruiters,
    "teamtailor": teamtailor,
    "workday": workday,
    "jsonld": jsonld,
}
