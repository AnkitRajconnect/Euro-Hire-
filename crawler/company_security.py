"""Publication gates for company identity, approved career sources and vacancies.

The rules are deliberately fail-closed: missing evidence means do not publish.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from urllib.parse import urlparse

APPROVAL_METHODS = {
    "registry_registered_website",
    "siren_in_legal_notice",
    "linked_from_verified_company_domain",
    "employer_domain_challenge",
    "manual_documented_review",
    "public_ats_board_review",
}

ATS_HOSTS = {
    "greenhouse": {"boards.greenhouse.io", "job-boards.greenhouse.io", "job-boards.eu.greenhouse.io"},
    "lever": {"jobs.lever.co", "jobs.eu.lever.co", "eu.lever.co"},
    "ashby": {"jobs.ashbyhq.com"},
    "personio": set(),  # tenant-specific host is checked below
    "recruitee": set(),
    "workable": {"apply.workable.com"},
    "smartrecruiters": {"jobs.smartrecruiters.com"},
    "teamtailor": set(),  # tenant-specific host is checked below
    "workday": set(),  # tenant and wd-region are encoded in ats_slug
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
    """Confirm the configured careers URL is the ATS board for this exact slug."""
    ats = company.get("ats")
    slug = str(company.get("ats_slug") or company.get("slug") or "")
    url = company.get("career_url") or ((company.get("verification") or {}).get("source") or {}).get("career_url")
    if not slug or not is_https_url(url):
        return False
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = [part for part in parsed.path.split("/") if part]
    if ats in {"greenhouse", "lever", "ashby"}:
        return host in ATS_HOSTS[ats] and bool(path) and path[0].lower() == slug.lower()
    if ats == "workable":
        return host == "apply.workable.com" and bool(path) and path[0].lower() == slug.lower()
    return False


def is_company_approved(company: dict) -> tuple[bool, str | None]:
    """Require registry evidence or documented first-party public ATS provenance.

    Curated ATS entries are verified career-source records only. They are not
    represented as independently verified against a national company register.
    """
    v = company.get("verification") or {}
    if v.get("mode") == "curated_public_ats":
        source = v.get("source") or {}
        domain = v.get("domain") or {}
        if v.get("status") != "approved":
            return False, "curated employer source is not approved"
        if company.get("ats") not in {"greenhouse", "lever", "ashby", "workable"}:
            return False, "curated launch supports Greenhouse, Lever, Ashby and Workable only"
        official_domain = normalise_host(company.get("official_domain"))
        careers_url = company.get("official_careers_url")
        if not official_domain or not is_https_url(careers_url):
            return False, "official employer careers-page evidence is missing"
        careers_host = normalise_host(careers_url)
        if careers_host != official_domain and not careers_host.endswith("." + official_domain):
            return False, "careers-page evidence is outside the configured official domain"
        if domain.get("verified") is not True or normalise_host(domain.get("url")) != official_domain:
            return False, "official employer domain has not passed the curated source review"
        if source.get("approved") is not True or source.get("verified") is not True:
            return False, "public ATS career source has not passed review"
        if source.get("method") != "public_ats_board_review":
            return False, "unsupported curated source-review method"
        if not is_https_url(source.get("career_url")) or not _curated_board_matches(company):
            return False, "career URL does not match the configured public ATS board and company slug"
        evidence = source.get("evidence_url")
        if not is_https_url(evidence):
            return False, "official careers-page evidence URL is missing"
        evidence_host = normalise_host(evidence)
        if evidence_host != official_domain and not evidence_host.endswith("." + official_domain):
            return False, "career-source evidence is not on the official employer domain"
        checked_at = source.get("checked_at")
        try:
            checked_date = datetime.fromisoformat(str(checked_at).replace("Z", "+00:00")).date()
        except (TypeError, ValueError):
            try:
                checked_date = date.fromisoformat(str(checked_at))
            except (TypeError, ValueError):
                return False, "curated source review date is missing"
        age_days = (datetime.now(timezone.utc).date() - checked_date).days
        if age_days < -1:
            return False, "curated source review date is in the future"
        # The review date is versioned provenance in companies.yaml, not a
        # temporary credential. Keep the curated list usable beyond 90 days;
        # the public ATS endpoint itself is checked live on every crawler run.
        return True, None

    registry = v.get("registry") or {}
    domain = v.get("domain") or {}
    source = v.get("source") or {}

    if v.get("status") != "approved":
        return False, "company verification is not approved"
    if registry.get("verified") is not True or registry.get("status") != "active":
        return False, "active legal registration has not been verified"
    checked_at = registry.get("checked_at")
    try:
        checked_date = datetime.fromisoformat(str(checked_at).replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        try:
            checked_date = date.fromisoformat(str(checked_at))
        except (TypeError, ValueError):
            return False, "registry check date is missing"
    age_days = (datetime.now(timezone.utc).date() - checked_date).days
    if age_days < -1:
        return False, "registry check date is in the future"
    if age_days > 90:
        return False, "registry record is older than 90 days and must be refreshed"
    if registry.get("legal_person") is not True or not registry.get("country") or not registry.get("id"):
        return False, "a registered legal entity is required; natural-person records are excluded"
    def fresh_check(value: str | None, label: str, max_days: int = 90) -> tuple[bool, str | None]:
        try:
            checked = datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
        except (TypeError, ValueError):
            try:
                checked = date.fromisoformat(str(value))
            except (TypeError, ValueError):
                return False, f"{label} check date is missing"
        age = (datetime.now(timezone.utc).date() - checked).days
        if age < -1:
            return False, f"{label} check date is in the future"
        if age > max_days:
            return False, f"{label} check is older than {max_days} days and must be refreshed"
        return True, None

    if domain.get("verified") is not True or not normalise_host(domain.get("url")):
        return False, "official company domain has not been verified"
    if domain.get("method") not in APPROVAL_METHODS:
        return False, "unsupported company-domain verification method"
    ok, reason = fresh_check(domain.get("checked_at"), "official-domain")
    if not ok:
        return False, reason
    if source.get("approved") is not True or source.get("verified") is not True:
        return False, "career source has not been approved"
    if source.get("method") not in APPROVAL_METHODS:
        return False, "career source approval evidence is missing"
    ok, reason = fresh_check(source.get("checked_at"), "career-source")
    if not ok:
        return False, reason
    if not is_https_url(source.get("career_url")):
        return False, "approved career URL must use HTTPS"
    if company.get("ats") not in ATS_HOSTS:
        return False, "unsupported applicant tracking system"
    if not (company.get("ats_slug") or company.get("slug")):
        return False, "career board identifier is missing"
    return True, None


def _slug_path_matches(path: str, slug: str) -> bool:
    parts = [p for p in path.split("/") if p]
    if not parts:
        return False
    # Career-board IDs are the first path segment on the supported hosted boards.
    return parts[0].lower() == slug.lower()


def validate_vacancy_source(job_url: str, company: dict) -> tuple[bool, str | None]:
    """Ensure a vacancy URL belongs to the already-approved company career feed."""
    if not is_https_url(job_url):
        return False, "vacancy URL is not a safe HTTPS URL"
    parsed = urlparse(job_url)
    host = (parsed.hostname or "").lower()
    ats = company.get("ats")
    raw_slug = str(company.get("ats_slug") or company.get("slug") or "")
    slug = raw_slug.lower()

    official_host = normalise_host(company.get("official_domain"))
    if ats == "jsonld":
        allowed = {official_host}
        allowed.update(normalise_host(h) for h in (company.get("approved_hosts") or []))
        return (host in allowed, None if host in allowed else "vacancy URL is outside approved company domain")

    # Some ATS feeds return the employer's own careers URL instead of the ATS
    # hostname. Accept only the explicitly reviewed company domain.
    if official_host and (host == official_host or host.endswith("." + official_host)):
        return True, None

    if ats == "personio":
        allowed = host in {f"{slug}.jobs.personio.de", f"{slug}.jobs.personio.com"}
    elif ats == "workable":
        # Workable's account widget can return either the tenant-qualified URL
        # (/account/j/SHORTCODE/...) or its canonical short URL (/j/SHORTCODE).
        # The latter is safe here because the posting was fetched from this
        # exact account's public widget endpoint above.
        parts = [p for p in parsed.path.split("/") if p]
        allowed = (host == "apply.workable.com" and (
            (len(parts) >= 2 and parts[0].lower() == slug)
            or (len(parts) >= 2 and parts[0].lower() == "j" and bool(parts[1]))
        ))
    elif ats == "recruitee":
        allowed = host == f"{slug}.recruitee.com"
    elif ats == "teamtailor":
        allowed = host == f"{slug}.teamtailor.com"
    elif ats == "workday":
        # Workday site identifiers are case-sensitive. Preserve the exact
        # career-site slug captured from the employer's official careers link.
        parts = raw_slug.split("|")
        if len(parts) != 4:
            return False, "Workday board identifier is invalid"
        tenant, region, site, locale = parts
        allowed_host = f"{tenant}.{region}.myworkdayjobs.com"
        path_parts = [p for p in parsed.path.split("/") if p]
        allowed = (host == allowed_host and len(path_parts) >= 3
                   and path_parts[0].lower() == locale.lower()
                   and path_parts[1] == site and path_parts[2].lower() == "job")
    elif ats in ATS_HOSTS:
        allowed = host in ATS_HOSTS[ats] and _slug_path_matches(parsed.path, slug)
    else:
        allowed = False
    return (allowed, None if allowed else "vacancy URL does not match the approved career-board host and identifier")


# Strong signals that a posting is not a normal professional vacancy. These are
# applied to the title first, not incidental mentions in the full description.
EXCLUDED_TITLE_RE = re.compile(
    r"\b(plumb(?:er|ing)|electrician|carpenter|bricklayer|mason|roofer|painter and decorator|"
    r"construction labourer|construction laborer|general labourer|general laborer|labourer|laborer|"
    r"warehouse operative|picker.?packer|order picker|packing operative|delivery driver|truck driver|"
    r"courier|cleaner|cleaning operative|housekeeper|housekeeping|kitchen porter|dishwasher|"
    r"waiter|waitress|barista|cook|head chef|sous chef|chef de cuisine|chef cuisinier|gardener|groundskeeper|handyperson|maintenance operative|"
    r"security guard|care assistant|home carer|personal care assistant|service technician|field technician|machine operator|"
    r"plombier|électricien|electricien|maçon|macon|charpentier|ouvrier|manoeuvre|agent de nettoyage|nettoyeur|"
    r"préparateur de commandes|preparateur de commandes|chauffeur-livreur|livreur|serveur de restaurant|serveuse|cuisinier|plongeur|jardinier|"
    r"loodgieter|elektricien|timmerman|metselaar|bouwvakker|schoonmaker|magazijnmedewerker|orderpicker|bezorger|vrachtwagenchauffeur|kelner|kok|"
    r"klempner|zimmermann|maurer|bauarbeiter|reinigungskraft|lagerarbeiter|kommissionierer|lieferfahrer|kurierfahrer|kellner|koch)\b",
    re.I,
)

FRAUD_REJECT_RE = re.compile(
    r"\b(application fee|recruitment fee|pay to apply|pay a fee to apply|upfront (?:fee|payment)|"
    r"send (?:us )?money|wire (?:us )?money|gift cards?|crypto(?:currency)? payment|bitcoin payment|"
    r"pay (?:for|to) (?:a )?(?:background check|security check|training certificate)|"
    r"purchase (?:a )?(?:starter kit|equipment package) before (?:starting|employment)|"
    r"frais de candidature|frais de recrutement|payer pour postuler|kosten voor sollicitatie|"
    r"sollicitatiekosten|bewerbungsgebühr|bewerbungsgebuehr|geld überweisen|geld ueberweisen)\b",
    re.I,
)
FRAUD_REVIEW_RE = re.compile(
    r"\b(whats ?app|telegram|signal app|personal bank account|credit card details|"
    r"passport copy before interview|identity document before interview|"
    r"send (?:your )?(?:passport|national id|social security number) before (?:an )?interview|"
    r"envoyez (?:votre )?passeport avant entretien|identiteitsbewijs voor gesprek|ausweiskopie vor interview)\b",
    re.I,
)


def screen_vacancy(raw: dict, company: dict) -> tuple[str, list[str]]:
    """Return ('publish'|'reject'|'review', reasons)."""
    title = (raw.get("title") or "").strip()
    description = raw.get("description") or ""
    url = raw.get("url") or ""
    if EXCLUDED_TITLE_RE.search(title):
        return "reject", ["excluded manual/service occupation"]
    ok, reason = validate_vacancy_source(url, company)
    if not ok:
        return "reject", [reason or "unapproved vacancy URL"]
    blob = f"{title}\n{description[:12000]}"
    if FRAUD_REJECT_RE.search(blob):
        return "reject", ["vacancy contains a high-risk payment or money-transfer request"]
    if FRAUD_REVIEW_RE.search(blob):
        return "review", ["vacancy contains a recruitment-risk signal requiring human review"]
    from .normalize import classify
    role, _ = classify(title, raw.get("department"))
    if role == "other":
        return "review", ["occupation is not in the approved professional-role taxonomy"]
    return "publish", []
