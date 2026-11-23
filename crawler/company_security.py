"""Publication gates for reviewed public employer career boards and vacancies."""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from urllib.parse import urlparse

ATS_HOSTS = {
    "greenhouse": {"boards.greenhouse.io", "job-boards.greenhouse.io", "job-boards.eu.greenhouse.io"},
    "lever": {"jobs.lever.co", "jobs.eu.lever.co", "eu.lever.co"},
    "ashby": {"jobs.ashbyhq.com"},
    "personio": set(),
    "recruitee": set(),
    "workable": {"apply.workable.com"},
    "smartrecruiters": {"jobs.smartrecruiters.com"},
    "teamtailor": set(),
    "workday": set(),
    "jsonld": set(),
}


def normalise_host(value: str | None) -> str:
    if not value:
        return ""
    parsed = urlparse(value if "://" in value else f"https://{value}")
    return (parsed.hostname or "").lower().rstrip(".")


def is_https_url(value: str | None) -> bool:
    try:
        parsed = urlparse(value or "")
        return (parsed.scheme == "https" and bool(parsed.hostname) and
                not parsed.username and not parsed.password and parsed.port in (None, 443))
    except ValueError:
        return False


def _curated_board_matches(company: dict) -> bool:
    ats = company.get("ats")
    slug = str(company.get("ats_slug") or company.get("slug") or "")
    url = company.get("career_url") or ((company.get("verification") or {}).get("source") or {}).get("career_url")
    if not slug or not is_https_url(url):
        return False
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    parts = [part for part in parsed.path.split("/") if part]
    if ats == "greenhouse":
        return host in ATS_HOSTS[ats] and bool(parts) and parts[0].lower() == slug.lower()
    if ats == "lever":
        return host in ATS_HOSTS[ats] and bool(parts) and parts[0].lower() == slug.lower()
    if ats == "ashby":
        return host in ATS_HOSTS[ats] and bool(parts) and parts[0].lower() == slug.lower()
    if ats == "workable":
        return host == "apply.workable.com" and bool(parts) and parts[0].lower() == slug.lower()
    return False


def is_company_approved(company: dict) -> tuple[bool, str | None]:
    """Require the public board to be linked to the employer's own careers page."""
    verification = company.get("verification") or {}
    if verification.get("mode") != "curated_public_ats" or verification.get("status") != "approved":
        return False, "employer career board has not been reviewed"
    if company.get("ats") not in {"greenhouse", "lever", "ashby", "workable"}:
        return False, "launch source must use a reviewed Greenhouse, Lever, Ashby or Workable board"

    official_domain = normalise_host(company.get("official_domain"))
    careers_url = company.get("official_careers_url")
    if not official_domain or not is_https_url(careers_url):
        return False, "official employer careers-page evidence is missing"
    careers_host = normalise_host(careers_url)
    if careers_host != official_domain and not careers_host.endswith("." + official_domain):
        return False, "careers-page evidence is outside the employer domain"

    domain = verification.get("domain") or {}
    if domain.get("verified") is not True or normalise_host(domain.get("url")) != official_domain:
        return False, "official domain has not passed review"
    source = verification.get("source") or {}
    if source.get("approved") is not True or source.get("verified") is not True:
        return False, "public ATS board has not passed review"
    if source.get("method") != "public_ats_board_review" or not _curated_board_matches(company):
        return False, "configured career URL does not match the public ATS board and slug"

    evidence = source.get("evidence_url")
    if not is_https_url(evidence):
        return False, "official careers-page evidence URL is missing"
    evidence_host = normalise_host(evidence)
    if evidence_host != official_domain and not evidence_host.endswith("." + official_domain):
        return False, "source evidence is not on the employer's own domain"

    checked_at = source.get("checked_at")
    try:
        checked_date = datetime.fromisoformat(str(checked_at).replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        try:
            checked_date = date.fromisoformat(str(checked_at))
        except (TypeError, ValueError):
            return False, "source review date is missing"
    age_days = (datetime.now(timezone.utc).date() - checked_date).days
    if age_days < -1:
        return False, "source review date is in the future"
    return True, None


def validate_vacancy_source(url: str, company: dict) -> tuple[bool, str | None]:
    """Ensure each vacancy URL belongs to the expected employer board."""
    if not is_https_url(url):
        return False, "job URL must use HTTPS"
    host = normalise_host(url)
    ats = company.get("ats")
    slug = str(company.get("ats_slug") or company.get("slug") or "")
    parts = [part for part in urlparse(url).path.split("/") if part]
    if ats in {"greenhouse", "lever", "ashby", "workable"}:
        expected_hosts = ATS_HOSTS[ats]
        if host not in expected_hosts:
            return False, "vacancy URL is not on the configured ATS host"
        if ats == "workable" and parts and parts[0] == "j":
            return True, None
        if not parts or parts[0].lower() != slug.lower():
            return False, "vacancy URL belongs to a different board"
        return True, None
    if ats == "personio":
        if not host.endswith(".jobs.personio.de") and not host.endswith(".jobs.personio.com"):
            return False, "vacancy URL is not on Personio"
        return host.split(".")[0] == slug, None
    if ats == "recruitee":
        return host == f"{slug}.recruitee.com", None
    if ats == "teamtailor":
        return host == f"{slug}.teamtailor.com", None
    if ats == "smartrecruiters":
        return host == "jobs.smartrecruiters.com" and bool(parts) and parts[0].lower() == slug.lower(), None
    if ats == "workday":
        tenant, region, site, _locale = (slug.split("|", 3) + ["", "", "", ""])[:4]
        return host == f"{tenant}.{region}.myworkdayjobs.com" and bool(parts) and parts[0] == site, None
    if ats == "jsonld":
        official = normalise_host(company.get("official_domain"))
        return host == official or host.endswith("." + official), None
    return False, "unsupported ATS"


SCAM_REVIEW_RE = re.compile(r"\b(whatsapp|telegram|signal|personal gmail|text me|dm me)\b", re.I)
PAYMENT_RE = re.compile(r"\b(application fee|pay (?:a )?fee|processing fee|deposit to apply|money transfer|buy equipment from us)\b", re.I)
MANUAL_TRADE_RE = re.compile(
    r"\b(plumber|electrician|roofer|carpenter|bricklayer|welder|mechanic|delivery driver|"
    r"cleaner|cleaning operative|warehouse operative|forklift driver|waiter|waitress|"
    r"construction labourer|laborer|driver|chef|cook|security guard|nanny|caregiver|"
    r"plombier|électricien|electricien|couvreur|charpentier|maçon|macon|livreur|"
    r"nettoyeur|schoonmaker|loodgieter|klempner)\b", re.I
)


def screen_vacancy(raw: dict, company: dict) -> tuple[str, list[str]]:
    title = (raw.get("title") or "").strip()
    url = raw.get("url") or ""
    if not title:
        return "reject", ["missing job title"]
    if not validate_vacancy_source(url, company)[0]:
        return "reject", ["vacancy URL is outside the approved employer board"]
    blob = f"{title} {raw.get('description') or ''}"
    if MANUAL_TRADE_RE.search(title):
        return "reject", ["manual or service occupation is outside the launch scope"]
    if PAYMENT_RE.search(blob):
        return "reject", ["vacancy requests payment or money transfer"]
    if SCAM_REVIEW_RE.search(blob):
        return "review", ["vacancy contains a recruitment-risk signal"]
    return "publish", []
