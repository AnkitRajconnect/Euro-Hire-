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
from urllib.parse import urljoin
from datetime import datetime, timezone

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
    """Fallback for custom career pages that embed schema.org JobPosting data.
    `slug` is unused; give `urls: [...]` in companies.yaml. Honors robots.txt."""
    out = []
    for url in urls or []:
        if not http.allowed_by_robots(url):
            raise FetchError(f"robots.txt disallows {url}")
        page = http.get_text(url)
        for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page, re.S | re.I):
            try:
                blob = json.loads(m.group(1))
            except ValueError:
                continue
            nodes = blob if isinstance(blob, list) else blob.get("@graph", [blob])
            for n in nodes:
                if not isinstance(n, dict) or n.get("@type") != "JobPosting":
                    continue
                loc = n.get("jobLocation")
                loc = loc[0] if isinstance(loc, list) and loc else (loc or {})
                addr = (loc.get("address") or {}) if isinstance(loc, dict) else {}
                out.append({
                    "id": str(n.get("identifier", {}).get("value") if isinstance(n.get("identifier"), dict)
                              else n.get("identifier") or n.get("url") or n.get("title")),
                    "title": n.get("title", ""),
                    "url": n.get("url") or url,
                    "location": ", ".join(x for x in (addr.get("addressLocality"), addr.get("addressCountry")) if isinstance(x, str)),
                    "remote": True if n.get("jobLocationType") == "TELECOMMUTE" else None,
                    "posted": _date(n.get("datePosted")),
                    "description": strip_html(n.get("description")),
                })
    return out



# Public board endpoints beyond the first launch set.
# None of these functions uses an employer API key or private recruiting token.

def _board_items(payload, *keys):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _plain(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return ", ".join(str(value.get(k)) for k in ("name", "city", "region", "country") if value.get(k))
    if isinstance(value, list):
        return ", ".join(part for part in (_plain(x) for x in value) if part)
    return str(value)


def breezy(slug, http, **_):
    payload = http.get_json(f"https://{slug}.breezy.hr/json")
    out = []
    for job in _board_items(payload, "jobs", "positions"):
        title = job.get("name") or job.get("title") or ""
        job_id = str(job.get("id") or job.get("position_id") or "")
        if not title or not job_id:
            continue
        url = job.get("url") or job.get("career_url") or f"https://{slug}.breezy.hr/p/{job_id}"
        out.append({
            "id": job_id, "title": title, "url": url,
            "location": _plain(job.get("location") or job.get("locations")),
            "department": job.get("department") or job.get("team"),
            "posted": _date(job.get("published_at") or job.get("created_at")),
            "description": strip_html(job.get("description")),
            "remote": True if job.get("remote") else None,
        })
    return out


def pinpoint(slug, http, **_):
    base = f"https://{slug}.pinpointhq.com"
    payload = http.get_json(f"{base}/postings.json")
    out = []
    for job in _board_items(payload, "postings", "jobs", "data"):
        job_id = str(job.get("id") or job.get("job_id") or "")
        title = job.get("title") or job.get("name") or ""
        if not job_id or not title:
            continue
        url = job.get("url") or job.get("posting_url") or f"{base}/postings/{job_id}"
        out.append({
            "id": job_id, "title": title, "url": urljoin(base, url),
            "location": _plain(job.get("location") or job.get("locations")),
            "department": job.get("department") or job.get("team"),
            "posted": _date(job.get("published_at") or job.get("created_at")),
            "description": strip_html(job.get("description") or job.get("content")),
            "remote": True if job.get("remote") else None,
        })
    return out


def bamboohr(slug, http, **_):
    payload = http.get_json(f"https://{slug}.bamboohr.com/careers/list",
                            headers={"Accept": "application/json"})
    out = []
    for job in _board_items(payload, "result", "jobs", "positions"):
        job_id = str(job.get("id") or job.get("jobId") or "")
        title = job.get("jobOpeningName") or job.get("title") or job.get("name") or ""
        if not job_id or not title:
            continue
        url = job.get("url") or job.get("jobUrl") or f"https://{slug}.bamboohr.com/careers/{job_id}"
        out.append({
            "id": job_id, "title": title, "url": urljoin(f"https://{slug}.bamboohr.com", url),
            "location": _plain(job.get("location") or job.get("locationName")),
            "department": job.get("departmentLabel") or job.get("department"),
            "posted": _date(job.get("datePosted") or job.get("createdAt")),
            "description": strip_html(job.get("description") or job.get("jobDescription")),
        })
    return out


def jazzhr(slug, http, **_):
    text = http.get_text(f"https://app.jazz.co/feeds/export/jobs/{slug}")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise FetchError(f"invalid JazzHR XML for {slug}: {exc}") from exc
    out = []
    for node in root.iter():
        if node.tag.split("}")[-1].lower() != "job":
            continue
        fields = {child.tag.split("}")[-1].lower(): (child.text or "").strip() for child in node}
        job_id = fields.get("id") or fields.get("jobid") or fields.get("job_id")
        title = fields.get("title") or fields.get("name") or ""
        if not job_id or not title:
            continue
        url = fields.get("url") or fields.get("applyurl") or f"https://{slug}.applytojob.com/apply/{job_id}"
        out.append({
            "id": str(job_id), "title": title, "url": url,
            "location": fields.get("location") or ", ".join(
                value for value in (fields.get("city"), fields.get("state"), fields.get("country")) if value),
            "department": fields.get("department"),
            "posted": _date(fields.get("dateposted") or fields.get("createdat")),
            "description": strip_html(fields.get("description")),
        })
    return out


def keka(slug, http, **_):
    careers_url = f"https://{slug}.keka.com/careers"
    if not http.allowed_by_robots(careers_url):
        raise FetchError(f"robots.txt disallows Keka career page: {careers_url}")
    page = http.get_text(careers_url)
    match = re.search(r"/ats/documents/([a-f0-9-]+)/careerportal", page, re.I)
    if not match:
        raise FetchError(f"Keka public career-feed identifier not found for {slug}")
    feed_url = f"https://{slug}.keka.com/careers/api/embedjobs/default/active/{match.group(1)}"
    payload = http.get_json(feed_url)
    out = []
    for job in _board_items(payload, "jobs", "data", "result"):
        job_id = str(job.get("id") or job.get("jobId") or job.get("jobOpeningId") or "")
        title = job.get("title") or job.get("jobTitle") or ""
        if not job_id or not title:
            continue
        out.append({
            "id": job_id, "title": title,
            "url": job.get("url") or f"https://{slug}.keka.com/careers/jobdetails/{job_id}",
            "location": _plain(job.get("location") or job.get("locations")),
            "department": job.get("department") or job.get("departmentName"),
            "posted": _date(job.get("postedAt") or job.get("createdAt")),
            "description": strip_html(job.get("description") or job.get("jobDescription")),
            "remote": True if job.get("isRemote") or job.get("remote") else None,
        })
    return out


def homerun(slug, http, **_):
    text = http.get_text(f"https://feed.homerun.co/{slug}")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise FetchError(f"invalid Homerun feed for {slug}: {exc}") from exc
    out = []
    for entry in root.iter():
        if entry.tag.split("}")[-1].lower() != "entry":
            continue
        title = (entry.findtext("{*}title") or "").strip()
        job_id = (entry.findtext("{*}id") or "").strip()
        link = ""
        for node in entry.iter():
            if node.tag.split("}")[-1].lower() == "link":
                link = node.attrib.get("href") or (node.text or "").strip()
                if link:
                    break
        if not title or not link:
            continue
        out.append({
            "id": job_id or link, "title": title, "url": link,
            "location": "", "posted": _date(entry.findtext("{*}published") or entry.findtext("{*}updated")),
            "description": strip_html(entry.findtext("{*}content") or entry.findtext("{*}summary")),
        })
    return out


# Include an explicit allowlist of public, no-key board readers. Token-only
# corporate APIs (for example, Zoho Recruit and Freshteam private APIs) are not included.

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
    "breezy": breezy,
    "pinpoint": pinpoint,
    "bamboohr": bamboohr,
    "jazzhr": jazzhr,
    "keka": keka,
    "homerun": homerun,
    "jsonld": jsonld,
}
