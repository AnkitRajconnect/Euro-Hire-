"""Validate public ATS board IDs and HTTPS vacancy links."""
from __future__ import annotations

import re
from urllib.parse import urlparse

PUBLIC_ATS = {
    "greenhouse", "lever", "ashby", "personio", "recruitee", "workable",
    "smartrecruiters", "teamtailor", "workday", "breezy", "pinpoint",
    "bamboohr", "jazzhr", "keka", "homerun",
}

SCAM_REVIEW_RE = re.compile(r"\b(whatsapp|telegram|signal|personal gmail|text me|dm me)\b", re.I)
PAYMENT_RE = re.compile(r"\b(application fee|pay (?:a )?fee|processing fee|deposit to apply|money transfer|buy equipment from us)\b", re.I)
MANUAL_TRADE_RE = re.compile(
    r"\b(plumber|electrician|roofer|carpenter|bricklayer|welder|mechanic|delivery driver|"
    r"cleaner|cleaning operative|warehouse operative|forklift driver|waiter|waitress|"
    r"construction labourer|laborer|driver|chef|cook|security guard|nanny|caregiver|"
    r"plombier|électricien|electricien|couvreur|charpentier|maçon|macon|livreur|"
    r"nettoyeur|schoonmaker|loodgieter|klempner)\b", re.I
)


def host_of(value: str | None) -> str:
    if not value:
        return ""
    return (urlparse(value if "://" in value else "https://" + value).hostname or "").lower().rstrip(".")


def is_https_url(value: str | None) -> bool:
    try:
        parsed = urlparse(value or "")
        return parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password and parsed.port in (None, 443)
    except ValueError:
        return False


def board_matches(company: dict) -> bool:
    ats = company.get("ats")
    slug = str(company.get("ats_slug") or company.get("slug") or "").lower()
    board_url = company.get("board_url") or ""
    if ats not in PUBLIC_ATS or not slug or not is_https_url(board_url):
        return False
    host = host_of(board_url)
    parts = [part for part in urlparse(board_url).path.split("/") if part]

    if ats == "greenhouse":
        return host in {"boards.greenhouse.io", "job-boards.greenhouse.io", "job-boards.eu.greenhouse.io"} and bool(parts) and parts[0].lower() == slug
    if ats == "lever":
        return host in {"jobs.lever.co", "jobs.eu.lever.co"} and bool(parts) and parts[0].lower() == slug
    if ats == "ashby":
        return host == "jobs.ashbyhq.com" and bool(parts) and parts[0].lower() == slug
    if ats == "workable":
        return host == "apply.workable.com" and bool(parts) and parts[0].lower() == slug
    if ats == "smartrecruiters":
        return host == "jobs.smartrecruiters.com" and bool(parts) and parts[0].lower() == slug
    if ats == "personio":
        return host in {slug + ".jobs.personio.de", slug + ".jobs.personio.com"}
    if ats == "recruitee":
        return host == slug + ".recruitee.com"
    if ats == "teamtailor":
        return host == slug + ".teamtailor.com"
    if ats == "workday":
        try:
            tenant, region, site, locale = slug.split("|", 3)
        except ValueError:
            return False
        return host == tenant + "." + region + ".myworkdayjobs.com" and site in parts
    if ats == "breezy":
        return host == slug + ".breezy.hr"
    if ats == "pinpoint":
        return host == slug + ".pinpointhq.com"
    if ats == "bamboohr":
        return host == slug + ".bamboohr.com"
    if ats == "jazzhr":
        return host == slug + ".applytojob.com" or host == "app.jazz.co"
    if ats == "keka":
        return host == slug + ".keka.com"
    if ats == "homerun":
        return host == "feed.homerun.co" and bool(parts) and parts[0].lower() == slug
    return False


def is_company_approved(company: dict) -> tuple[bool, str | None]:
    """A provider-valid board URL is the source gate; there is no company allowlist."""
    verification = company.get("verification") or {}
    if verification.get("mode") != "discovered_public_ats" or verification.get("status") != "approved":
        return False, "board was not discovered from the public ATS index"
    if not board_matches(company):
        return False, "board URL does not match the ATS provider and board ID"
    return True, None


def validate_vacancy_source(url: str, company: dict, trusted_feed: bool = False) -> tuple[bool, str | None]:
    """Validate HTTPS job URLs. ATS-returned custom apply links are trusted only
    after the public board itself has passed provider/board-ID validation.
    """
    if not is_https_url(url):
        return False, "vacancy URL must use HTTPS"
    if trusted_feed and is_company_approved(company)[0]:
        return True, None

    host = host_of(url)
    ats = company.get("ats")
    slug = str(company.get("ats_slug") or company.get("slug") or "").lower()
    parts = [part for part in urlparse(url).path.split("/") if part]

    if ats == "greenhouse":
        return host in {"boards.greenhouse.io", "job-boards.greenhouse.io", "job-boards.eu.greenhouse.io"} and bool(parts) and parts[0].lower() == slug, None
    if ats == "lever":
        return host in {"jobs.lever.co", "jobs.eu.lever.co"} and bool(parts) and parts[0].lower() == slug, None
    if ats == "ashby":
        return host == "jobs.ashbyhq.com" and bool(parts) and parts[0].lower() == slug, None
    if ats == "workable":
        return host == "apply.workable.com" and bool(parts) and (parts[0].lower() == slug or parts[0].lower() == "j"), None
    if ats == "smartrecruiters":
        return host == "jobs.smartrecruiters.com" and bool(parts) and parts[0].lower() == slug, None
    if ats == "personio":
        return host in {slug + ".jobs.personio.de", slug + ".jobs.personio.com"}, None
    if ats == "recruitee":
        return host == slug + ".recruitee.com", None
    if ats == "teamtailor":
        return host == slug + ".teamtailor.com", None
    if ats == "workday":
        tenant, region, site, _locale = (slug.split("|", 3) + ["", "", "", ""])[:4]
        return host == tenant + "." + region + ".myworkdayjobs.com" and site in parts, None
    if ats == "breezy":
        return host == slug + ".breezy.hr", None
    if ats == "pinpoint":
        return host == slug + ".pinpointhq.com", None
    if ats == "bamboohr":
        return host == slug + ".bamboohr.com", None
    if ats == "jazzhr":
        return host == slug + ".applytojob.com" or host == "app.jazz.co", None
    if ats == "keka":
        return host == slug + ".keka.com", None
    if ats == "homerun":
        return host == "feed.homerun.co" and bool(parts) and parts[0].lower() == slug, None
    return False, "unsupported public ATS"


def screen_vacancy(raw: dict, company: dict) -> tuple[str, list[str]]:
    title = (raw.get("title") or "").strip()
    if not title:
        return "reject", ["missing job title"]
    trusted_feed = raw.get("_source_feed_validated") is True
    if not validate_vacancy_source(raw.get("url") or "", company, trusted_feed)[0]:
        return "reject", ["vacancy URL is outside the verified public ATS feed"]
    if MANUAL_TRADE_RE.search(title):
        return "reject", ["manual or service occupation is outside the launch scope"]
    blob = title + " " + (raw.get("description") or "")
    if PAYMENT_RE.search(blob):
        return "reject", ["vacancy requests payment or money transfer"]
    if SCAM_REVIEW_RE.search(blob):
        return "review", ["vacancy contains a recruitment-risk signal"]
    return "publish", []
