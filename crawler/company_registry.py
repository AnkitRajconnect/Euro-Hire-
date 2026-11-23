"""Company registry and career-source approval database.

Registry records are *candidates*, not automatic permission to crawl. A company
must be an active legal person, have a verified official domain, and have an
approved career source before it can enter the crawler's approved queue.

Examples:
  python -m crawler.company_registry init
  INSEE_SIRENE_API_KEY=... python -m crawler.company_registry search --country FR --query 'denominationUniteLegale:ORANGE'
  KVK_API_KEY=... python -m crawler.company_registry search --country NL --query 'ASML'
  python -m crawler.company_registry inspect-cbe --file /path/to/KboOpenData_Full.zip
  python -m crawler.company_registry import-cbe --file /path/to/KboOpenData_Full.zip --mapping cbe-mapping.json --confirm-full-snapshot
  python -m crawler.company_registry set-domain --country FR --registry-id 123456789 --domain https://example.fr --confirm
  python -m crawler.company_registry discover-careers --country FR --registry-id 123456789
  python -m crawler.company_registry approve-source --country FR --registry-id 123456789 --ats greenhouse --slug example --career-url https://example.fr/careers --method linked_from_verified_company_domain
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import os
import re
import sqlite3
import sys
import zipfile
from contextlib import contextmanager
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

from .discover import detect
from .http import FetchError, Http

DEFAULT_DB = Path("data/company_registry.sqlite3")
INSEE_BASE = "https://api.insee.fr/api-sirene/3.11"
COMPANIES_HOUSE_SEARCH = "https://api.company-information.service.gov.uk/search/companies"
CRO_DATASET_ID = "3fef41bc-b8f4-4b10-8434-ce51c29b1bba"
CRO_SEARCH = "https://opendata.cro.ie/api/3/action/datastore_search"
PRH_SEARCH = "https://avoindata.prh.fi/opendata-ytj-api/v3/companies"
BRREG_SEARCH = "https://data.brreg.no/enhetsregisteret/api/enheter"
KVK_SEARCH = "https://api.kvk.nl/api/v2/zoeken"
KVK_PROFILE = "https://api.kvk.nl/api/v1/basisprofielen"
CAREER_TERMS = re.compile(r"career|careers|job|jobs|vacanc|werken.?bij|recrut|emploi|karriere|stellenangebot|offre.?d'emploi|nous.?rejoindre", re.I)

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS companies (
  id INTEGER PRIMARY KEY,
  country TEXT NOT NULL,
  registry_id TEXT NOT NULL,
  legal_name TEXT NOT NULL,
  trading_name TEXT,
  legal_form TEXT,
  legal_person INTEGER NOT NULL DEFAULT 0,
  registry_status TEXT NOT NULL,
  registry_source TEXT NOT NULL,
  registry_checked_at TEXT,
  official_domain TEXT,
  domain_verified INTEGER NOT NULL DEFAULT 0,
  domain_method TEXT,
  domain_checked_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  raw_json TEXT,
  career_discovery_checked_at TEXT,
  career_discovery_attempted_at TEXT,
  career_discovery_status TEXT,
  UNIQUE(country, registry_id)
);
CREATE TABLE IF NOT EXISTS career_sources (
  id INTEGER PRIMARY KEY,
  company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  ats TEXT NOT NULL,
  ats_slug TEXT NOT NULL,
  career_url TEXT NOT NULL,
  evidence_url TEXT,
  evidence_type TEXT,
  approved INTEGER NOT NULL DEFAULT 0,
  approval_method TEXT,
  approved_at TEXT,
  last_checked TEXT,
  last_status TEXT,
  raw_json TEXT,
  UNIQUE(company_id, ats, ats_slug)
);
CREATE INDEX IF NOT EXISTS idx_companies_status ON companies(registry_status, legal_person, domain_verified);
CREATE INDEX IF NOT EXISTS idx_sources_approved ON career_sources(approved);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db: Path = DEFAULT_DB) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    # Lightweight migrations for databases created by an earlier GullyHire build.
    columns = {row["name"] for row in con.execute("PRAGMA table_info(companies)")}
    if "registry_checked_at" not in columns:
        con.execute("ALTER TABLE companies ADD COLUMN registry_checked_at TEXT")
        con.execute("UPDATE companies SET registry_checked_at=updated_at WHERE registry_checked_at IS NULL")
    if "career_discovery_checked_at" not in columns:
        con.execute("ALTER TABLE companies ADD COLUMN career_discovery_checked_at TEXT")
    if "career_discovery_status" not in columns:
        con.execute("ALTER TABLE companies ADD COLUMN career_discovery_status TEXT")
    if "career_discovery_attempted_at" not in columns:
        con.execute("ALTER TABLE companies ADD COLUMN career_discovery_attempted_at TEXT")
    con.commit()
    return con


def upsert_candidate(con: sqlite3.Connection, *, country: str, registry_id: str, legal_name: str,
                     registry_status: str, registry_source: str, legal_person: bool,
                     legal_form: str | None = None, trading_name: str | None = None,
                     official_domain: str | None = None, domain_verified: bool = False,
                     domain_method: str | None = None, raw: dict | None = None,
                     registry_checked_at: str | None = None, commit: bool = True) -> None:
    stamp = now_iso()
    checked_at = registry_checked_at or stamp
    if official_domain:
        parsed_domain = urlparse(official_domain if "://" in official_domain else f"https://{official_domain}")
        official_domain = (parsed_domain.hostname or "").lower() or None
    existing = con.execute("SELECT id, official_domain, legal_name FROM companies WHERE country=? AND registry_id=?",
                            (country.upper(), str(registry_id))).fetchone()
    domain_changed = bool(existing and official_domain and existing["official_domain"] and
                          official_domain != existing["official_domain"])
    def name_key(value: str) -> str:
        return " ".join((value or "").casefold().split())
    identity_changed = bool(existing and name_key(existing["legal_name"]) != name_key(legal_name))
    if domain_changed or identity_changed:
        # An official-name or registered-domain change invalidates the earlier
        # identity-to-domain relationship and every career-source approval.
        con.execute("UPDATE career_sources SET approved=0, approval_method=NULL, approved_at=NULL, last_status='identity_changed_reapproval_required' WHERE company_id=?",
                    (existing["id"],))
        con.execute("""UPDATE companies SET domain_verified=0, domain_method=NULL, domain_checked_at=NULL,
                    career_discovery_checked_at=NULL, career_discovery_attempted_at=NULL,
                    career_discovery_status='identity_changed' WHERE id=?""", (existing["id"],))
    con.execute("""INSERT INTO companies
      (country, registry_id, legal_name, trading_name, legal_form, legal_person, registry_status,
       registry_source, registry_checked_at, official_domain, domain_verified, domain_method, domain_checked_at,
       created_at, updated_at, raw_json)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(country, registry_id) DO UPDATE SET
       legal_name=excluded.legal_name, trading_name=COALESCE(excluded.trading_name, companies.trading_name),
       legal_form=COALESCE(excluded.legal_form, companies.legal_form),
       legal_person=excluded.legal_person, registry_status=excluded.registry_status,
       registry_source=excluded.registry_source,
       registry_checked_at=excluded.registry_checked_at,
       official_domain=COALESCE(excluded.official_domain, companies.official_domain),
       domain_verified=CASE WHEN excluded.official_domain IS NOT NULL AND excluded.official_domain != companies.official_domain
                            THEN excluded.domain_verified
                            WHEN excluded.domain_verified=1 THEN 1 ELSE companies.domain_verified END,
       domain_method=CASE WHEN excluded.official_domain IS NOT NULL AND excluded.official_domain != companies.official_domain
                          THEN excluded.domain_method ELSE COALESCE(excluded.domain_method, companies.domain_method) END,
       domain_checked_at=CASE WHEN excluded.official_domain IS NOT NULL AND excluded.official_domain != companies.official_domain
                              THEN excluded.domain_checked_at ELSE COALESCE(excluded.domain_checked_at, companies.domain_checked_at) END,
       updated_at=excluded.updated_at, raw_json=excluded.raw_json""",
       (country.upper(), str(registry_id), legal_name, trading_name, legal_form, int(legal_person),
        registry_status.lower(), registry_source, checked_at, official_domain, int(domain_verified), domain_method,
        stamp if domain_verified else None, stamp, stamp, json.dumps(raw or {}, ensure_ascii=False)))
    if commit:
        con.commit()


def _company(con: sqlite3.Connection, country: str, registry_id: str):
    return con.execute("SELECT * FROM companies WHERE country=? AND registry_id=?", (country.upper(), str(registry_id))).fetchone()


def _timestamp_is_fresh(value: str | None, max_age_days: int = 90) -> bool:
    try:
        checked = datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        return False
    age = (datetime.now(timezone.utc).date() - checked).days
    return -1 <= age <= max_age_days


def _registry_is_fresh(row: sqlite3.Row) -> bool:
    return _timestamp_is_fresh(row["registry_checked_at"])


def set_domain(con: sqlite3.Connection, country: str, registry_id: str, domain: str, confirmed: bool) -> None:
    from .company_security import is_https_url
    parsed = urlparse(domain if "://" in domain else f"https://{domain}")
    if (not is_https_url(parsed.geturl()) or not parsed.hostname or
            parsed.path not in ("", "/") or parsed.query or parsed.fragment or
            "." not in parsed.hostname or parsed.hostname.lower() in {"localhost", "example.com", "example.org"}):
        raise ValueError("Use a real, clean HTTPS company domain, e.g. https://company.example")
    row = _company(con, country, registry_id)
    if not row:
        raise ValueError("Company not found in registry database")
    new_host = parsed.hostname.lower()
    if row["official_domain"] and row["official_domain"] != new_host:
        con.execute("UPDATE career_sources SET approved=0, approval_method=NULL, approved_at=NULL, last_status='domain_changed_reapproval_required' WHERE company_id=?",
                    (row["id"],))
        con.execute("UPDATE companies SET career_discovery_checked_at=NULL, career_discovery_attempted_at=NULL, career_discovery_status='domain_changed' WHERE id=?",
                    (row["id"],))
    con.execute("""UPDATE companies SET official_domain=?, domain_verified=?, domain_method=?, domain_checked_at=?,
                career_discovery_checked_at=NULL, career_discovery_attempted_at=NULL,
                career_discovery_status='domain_reverified', updated_at=? WHERE id=?""",
                (new_host, int(confirmed), "manual_documented_review" if confirmed else "pending_manual_review",
                 now_iso() if confirmed else None, now_iso(), row["id"]))
    con.commit()


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self._href = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._href is not None:
            self.links.append((self._href, " ".join(self._text).strip()))
            self._href = None
            self._text = []


def discover_company_sources(con: sqlite3.Connection, country: str, registry_id: str, http: Http) -> list[dict]:
    row = _company(con, country, registry_id)
    if not row:
        raise ValueError("Company not found")
    if row["registry_status"] != "active" or not row["legal_person"]:
        raise ValueError("Only active registered legal entities can be considered")
    if not _registry_is_fresh(row):
        raise ValueError("Refresh the official registry record before discovering or approving career sources")
    if not row["domain_verified"] or not row["official_domain"]:
        raise ValueError("Verify the official company domain before discovering its career pages")
    if not _timestamp_is_fresh(row["domain_checked_at"]):
        raise ValueError("Refresh the official-domain verification before discovering or approving career sources")
    # Record an attempt before network access so a blocked or unavailable site
    # cannot monopolise every bounded discovery batch.
    con.execute("UPDATE companies SET career_discovery_attempted_at=?, career_discovery_status='checking' WHERE id=?",
                (now_iso(), row["id"]))
    con.commit()
    home = f"https://{row['official_domain']}"
    if not http.allowed_by_robots(home):
        raise ValueError("robots.txt disallows access to the official homepage; discovery stopped")
    html = http.get_text(home)
    parser = _Links()
    parser.feed(html)
    candidates: list[dict] = []
    pages = []
    # Some sites embed their ATS board in a script/iframe rather than an <a>.
    for ats, slug in detect(html):
        candidates.append({"ats": ats, "slug": slug, "career_url": home, "evidence_url": home,
                           "evidence_type": "embedded_in_verified_company_homepage", "auto_approve": True})
    root_host = (urlparse(home).hostname or "").lower().removeprefix("www.")
    def belongs_to_company(host: str) -> bool:
        host = (host or "").lower().removeprefix("www.")
        return host == root_host or host.endswith("." + root_host)
    for href, label in parser.links:
        full = urljoin(home, href)
        p = urlparse(full)
        if p.scheme != "https" or not p.hostname:
            continue
        if CAREER_TERMS.search(f"{label} {p.path}") and belongs_to_company(p.hostname):
            pages.append(full)
        # ATS links are evidence only when embedded in the verified domain's page.
        for ats, slug in detect(full):
            candidates.append({"ats": ats, "slug": slug, "career_url": full, "evidence_url": home,
                               "evidence_type": "linked_from_verified_company_domain", "auto_approve": True})
    for page_url in list(dict.fromkeys(pages))[:8]:
        if not http.allowed_by_robots(page_url):
            continue
        try:
            page_html = http.get_text(page_url)
        except FetchError:
            continue
        hits = detect(page_html)
        if hits:
            for ats, slug in hits:
                candidates.append({"ats": ats, "slug": slug, "career_url": page_url, "evidence_url": page_url,
                                   "evidence_type": "linked_from_verified_company_domain", "auto_approve": True})
        else:
            candidates.append({"ats": "jsonld", "slug": f"{country.lower()}-{registry_id}", "career_url": page_url,
                               "evidence_url": page_url, "evidence_type": "custom_career_page_needs_review"})
    # One board should have one canonical source row. Prefer the direct ATS
    # link from the company site over a generic homepage embed when both exist.
    by_board: dict[tuple[str, str], dict] = {}
    for item in candidates:
        key = (item["ats"], item["slug"])
        existing = by_board.get(key)
        if existing is None or (item.get("evidence_type") == "linked_from_verified_company_domain"
                                and existing.get("evidence_type") != "linked_from_verified_company_domain"):
            by_board[key] = item
    unique = list(by_board.values())
    for item in unique:
        auto_approved = bool(item.get("auto_approve"))
        approval_method = "linked_from_verified_company_domain" if auto_approved else None
        con.execute("""INSERT INTO career_sources
          (company_id, ats, ats_slug, career_url, evidence_url, evidence_type, approved,
           approval_method, approved_at, last_checked, last_status, raw_json)
          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
          ON CONFLICT(company_id, ats, ats_slug) DO UPDATE SET
            approved=CASE WHEN career_sources.career_url != excluded.career_url
                          THEN excluded.approved ELSE career_sources.approved END,
            approval_method=CASE WHEN career_sources.career_url != excluded.career_url
                                 THEN excluded.approval_method ELSE COALESCE(career_sources.approval_method, excluded.approval_method) END,
            approved_at=CASE WHEN career_sources.career_url != excluded.career_url
                             THEN excluded.approved_at ELSE COALESCE(career_sources.approved_at, excluded.approved_at) END,
            career_url=excluded.career_url, evidence_url=excluded.evidence_url,
            evidence_type=excluded.evidence_type, last_checked=excluded.last_checked,
            last_status=CASE WHEN excluded.approved=1 THEN 'auto_approved_official_company_link'
                             WHEN career_sources.approved=1 THEN career_sources.last_status
                             ELSE 'discovered_pending_review' END,
            raw_json=excluded.raw_json""",
          (row["id"], item["ats"], item["slug"], item["career_url"], item["evidence_url"], item["evidence_type"],
           int(auto_approved), approval_method, now_iso() if auto_approved else None, now_iso(),
           "auto_approved_official_company_link" if auto_approved else "discovered_pending_review",
           json.dumps(item, ensure_ascii=False)))
    discovered_keys = {(item["ats"], item["slug"]) for item in unique}
    existing_sources = con.execute("SELECT id,ats,ats_slug FROM career_sources WHERE company_id=? AND approved=1",
                                   (row["id"],)).fetchall()
    for existing_source in existing_sources:
        key = (existing_source["ats"], existing_source["ats_slug"])
        if key not in discovered_keys:
            con.execute("UPDATE career_sources SET approved=0, approval_method=NULL, approved_at=NULL, last_checked=?, last_status='no_longer_linked_from_verified_company_site' WHERE id=?",
                        (now_iso(), existing_source["id"]))
    con.execute("UPDATE companies SET career_discovery_checked_at=?, career_discovery_status=? WHERE id=?",
                (now_iso(), "sources_found" if unique else "no_career_source_found", row["id"]))
    con.commit()
    return unique


def approve_source(con: sqlite3.Connection, country: str, registry_id: str, ats: str, slug: str,
                   career_url: str, method: str) -> None:
    row = _company(con, country, registry_id)
    if not row:
        raise ValueError("Company not found")
    if row["registry_status"] != "active" or not row["legal_person"] or not row["domain_verified"]:
        raise ValueError("Company must be an active legal person with a verified official domain")
    if not _registry_is_fresh(row):
        raise ValueError("Refresh the official registry record before approving a career source")
    source = con.execute("SELECT * FROM career_sources WHERE company_id=? AND ats=? AND ats_slug=?",
                         (row["id"], ats, slug)).fetchone()
    if not source:
        raise ValueError("Source was not discovered from the verified company domain. Run discover-careers first.")
    from .company_security import is_https_url
    discovered_url = urlparse(source["career_url"])
    selected_url = urlparse(career_url)
    if (not is_https_url(career_url) or selected_url.hostname != discovered_url.hostname
            or selected_url.path != discovered_url.path or selected_url.query != discovered_url.query):
        raise ValueError("The approved career URL must be the exact HTTPS URL discovered from the verified company domain")
    if method not in {"linked_from_verified_company_domain", "registry_registered_website", "employer_domain_challenge", "manual_documented_review"}:
        raise ValueError("Unsupported approval method")
    # We require the operator to explicitly attest to the source relationship.
    con.execute("UPDATE career_sources SET career_url=?, approved=1, approval_method=?, approved_at=?, last_checked=?, last_status='approved' WHERE id=?",
                (career_url, method, now_iso(), now_iso(), source["id"]))
    con.commit()


def _gleif_companies(query: str, limit: int, http: Http, country: str | None = None) -> list[dict]:
    """Search GLEIF's public LEI API without credentials.

    GLEIF provides legal-entity identity records, not an exhaustive register of
    all businesses. No domain is inferred from the entity name or LEI record.
    All results remain candidates and must pass the existing domain/career gates.
    """
    if len(query.strip()) < 2:
        raise ValueError("Use at least two characters for a GLEIF company search")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    results: list[dict] = []
    page = 1
    page_size = min(200, max(1, limit))
    desired_country = country.upper() if country else None
    while len(results) < limit:
        params = {
            "filter[entity.legalName]": query.strip(),
            "page[size]": page_size,
            "page[number]": page,
        }
        payload = http.get_json("https://api.gleif.org/api/v1/lei-records", params=params,
                                headers={"Accept": "application/vnd.api+json, application/json"})
        records = payload.get("data") or []
        if isinstance(records, dict):
            records = [records]
        if not records:
            break
        for item in records:
            attrs = item.get("attributes") or item
            entity = attrs.get("entity") or {}
            registration = attrs.get("registration") or {}
            address = entity.get("legalAddress") or {}
            legal_name_value = entity.get("legalName") or {}
            legal_name = (legal_name_value.get("name") if isinstance(legal_name_value, dict)
                          else str(legal_name_value or "")).strip()
            lei = str(attrs.get("lei") or item.get("id") or "").strip().upper()
            record_country = str(address.get("country") or "").strip().upper()
            if not lei or not legal_name or len(lei) != 20 or not record_country:
                continue
            if desired_country and record_country != desired_country:
                continue
            entity_state = str(entity.get("status") or "").strip().upper()
            lei_state = str(registration.get("status") or "").strip().upper()
            if entity_state == "ACTIVE" and lei_state == "ISSUED":
                status = "active"
            elif entity_state == "INACTIVE" or lei_state in {"RETIRED", "ANNULLED", "DUPLICATE", "CANCELLED", "MERGED"}:
                status = "inactive"
            else:
                # In particular, an ACTIVE entity with a LAPSED LEI is not
                # treated as fresh identity evidence for automatic intake.
                status = "pending"
            legal_form_data = entity.get("legalForm") or {}
            legal_form = (legal_form_data.get("other") or legal_form_data.get("id")
                          if isinstance(legal_form_data, dict) else str(legal_form_data or ""))
            results.append({
                "country": record_country,
                "registry_id": lei,
                "legal_name": legal_name,
                "legal_form": legal_form or None,
                "legal_person": True,
                "registry_status": status,
                "registry_source": "GLEIF Global LEI Index",
                "raw": {
                    "lei": lei,
                    "entity_status": entity_state,
                    "lei_registration_status": registration.get("status"),
                    "legal_address": address,
                    "registration_authority": entity.get("registeredAt") or entity.get("registrationAuthority"),
                    "national_registration_number": entity.get("registeredAs"),
                    "initial_registration_date": registration.get("initialRegistrationDate"),
                    "last_update_date": registration.get("lastUpdateDate"),
                    "next_renewal_date": registration.get("nextRenewalDate"),
                    "note": "GLEIF LEI record is candidate identity evidence only; no employer domain is supplied or verified.",
                },
            })
            if len(results) >= limit:
                break
        if len(records) < page_size or len(results) >= limit:
            break
        page += 1
        if page > 100:
            break
    return results


def _insee_companies(query: str, limit: int, http: Http) -> list[dict]:
    key = os.getenv("INSEE_SIRENE_API_KEY")
    if not key:
        raise ValueError("Set INSEE_SIRENE_API_KEY from the INSEE API portal")
    results = []
    offset = 0
    while len(results) < limit:
        params = {"q": f"({query}) AND categorieJuridiqueUniteLegale:[3000 TO 6999]",
                  "nombre": min(1000, limit - len(results)), "debut": offset}
        payload = http.get_json(f"{INSEE_BASE}/siren", params=params,
                                headers={"X-INSEE-Api-Key-Integration": key})
        records = payload.get("unitesLegales", [])
        if not records:
            break
        for item in records:
            ul = item.get("uniteLegale", item)
            siren = ul.get("siren") or item.get("siren")
            name = ul.get("denominationUniteLegale") or ul.get("denominationUsuelle1UniteLegale")
            legal_form = str(ul.get("categorieJuridiqueUniteLegale") or "")
            diffusion = str(ul.get("statutDiffusionUniteLegale") or "").upper()
            status_code = str(ul.get("etatAdministratifUniteLegale") or "").upper()
            if (not siren or not name or status_code not in {"A", "C"}
                    or not legal_form.startswith(tuple("3456")) or diffusion in {"P", "N"}):
                continue
            results.append({"country": "FR", "registry_id": str(siren), "legal_name": name,
                            "legal_form": legal_form, "legal_person": True,
                            "registry_status": "active" if status_code == "A" else "inactive",
                            "registry_source": "INSEE SIRENE", "raw": {
                                "legal_form_code": legal_form,
                                "principal_activity_code": ul.get("activitePrincipaleUniteLegale"),
                                "creation_date": ul.get("dateCreationUniteLegale"),
                            }})
        offset += len(records)
        if len(records) < params["nombre"] or offset >= int((payload.get("header") or {}).get("total", offset)):
            break
    return results


def _kvk_companies(query: str, limit: int, http: Http) -> list[dict]:
    key = os.getenv("KVK_API_KEY")
    if not key:
        raise ValueError("Set KVK_API_KEY after requesting a KVK API subscription")
    results = []
    page = 1
    while len(results) < limit and page <= 100:
        params = {"naam": query, "type": "rechtspersoon", "inclusiefInactieveRegistraties": "true",
                  "resultatenPerPagina": min(100, limit - len(results)), "pagina": page}
        payload = http.get_json(KVK_SEARCH, params=params, headers={"apikey": key})
        items = payload.get("resultaten", [])
        if not items:
            break
        for item in items:
            number = str(item.get("kvkNummer") or "")
            # The KVK API distinguishes legal entities from branches. Fail
            # closed if the response omits or contradicts the requested type.
            if not number or item.get("type") != "rechtspersoon":
                continue
            active = item.get("actief") in (True, "Ja", "ja", "true", "True")
            details = http.get_json(KVK_PROFILE, params={"kvkNummer": number}, headers={"apikey": key}) if active else {}
            legal_form = (details.get("rechtsvorm") or details.get("uitgebreideRechtsvorm")
                          or (details.get("eigenaar") or {}).get("rechtsvorm") or "")
            allowed_forms = {
                "besloten vennootschap", "naamloze vennootschap",
                "europese naamloze vennootschap", "coöperatie",
                "onderlinge waarborgmaatschappij",
            }
            # An active KVK record is not necessarily a commercial company:
            # foundations, associations and religious bodies are registered too.
            # Admit only known corporate forms; unknown forms require review.
            if active and str(legal_form).strip().casefold() not in allowed_forms:
                continue
            sites = ((details.get("hoofdvestiging") or {}).get("websites") or details.get("websites") or [])
            site = None
            if isinstance(sites, list) and sites:
                first = sites[0]
                site = first.get("website") or first.get("url") if isinstance(first, dict) else str(first)
            results.append({"country": "NL", "registry_id": number,
                            "legal_name": details.get("statutaireNaam") or details.get("naam") or item.get("naam"),
                            "legal_form": legal_form or None,
                            "legal_person": True, "registry_status": "active" if active else "inactive", "registry_source": "KVK Handelsregister",
                            "official_domain": site, "domain_verified": bool(site),
                            "domain_method": "registry_registered_website" if site else None,
                            "raw": {"kvk_number": number, "activities": details.get("sbiActiviteiten", [])}})
            if len(results) >= limit:
                break
        if len(items) < params["resultatenPerPagina"]:
            break
        page += 1
    return results


def _as_text(value) -> str:
    """Extract a safe display string from registry scalar/object fields."""
    if value is None:
        return ""
    if isinstance(value, dict):
        for key in ("value", "name", "description", "code", "type", "url", "website"):
            if value.get(key) not in (None, ""):
                return _as_text(value[key])
        return ""
    return str(value).strip()


def _companies_house_companies(query: str, limit: int, http: Http) -> list[dict]:
    key = os.getenv("COMPANIES_HOUSE_API_KEY")
    if not key:
        raise ValueError("Set COMPANIES_HOUSE_API_KEY from the free Companies House developer account")
    allowed_types = {
        "ltd", "plc", "llp", "scottish-limited-partnership",
        "overseas-company", "european-public-limited-liability-company-se",
        "private-limited-shares-section-30-exemption", "private-unlimited",
    }
    results, start = [], 0
    while len(results) < limit:
        page_size = min(100, limit - len(results))
        payload = http.get_json(COMPANIES_HOUSE_SEARCH,
                                params={"q": query, "items_per_page": page_size, "start_index": start},
                                auth=(key, ""))
        items = payload.get("items", [])
        if not isinstance(items, list) or not items:
            break
        for item in items:
            number = str(item.get("company_number") or "").strip()
            name = str(item.get("title") or item.get("company_name") or "").strip()
            company_type = str(item.get("company_type") or "").strip().lower()
            status = str(item.get("company_status") or "").strip().lower()
            # Companies House also indexes entities that are not commercial
            # employers. Unknown types remain outside the automatic intake.
            if not number or not name or company_type not in allowed_types:
                continue
            address = item.get("registered_office_address") or {}
            results.append({
                "country": "GB", "registry_id": number, "legal_name": name,
                "legal_form": company_type, "legal_person": True,
                "registry_status": "active" if status == "active" else "inactive",
                "registry_source": "UK Companies House",
                "raw": {"company_status": status, "company_type": company_type,
                        "registered_office_address": address,
                        "date_of_creation": item.get("date_of_creation")},
            })
            if len(results) >= limit:
                break
        start += len(items)
        total = int(payload.get("total_results") or start)
        if len(items) < page_size or start >= total:
            break
    return results


def _cro_companies(query: str, limit: int, http: Http) -> list[dict]:
    """Search the Irish CRO daily company snapshot (CC BY 4.0)."""
    results, offset = [], 0
    commercial_types = (
        "private company limited by shares", "designated activity company",
        "public limited company", "unlimited company", "societas europaea",
        "ltd", "dac", "plc", "ulc", "puc", "se",
    )
    while len(results) < limit:
        page_size = min(500, limit - len(results))
        payload = http.get_json(CRO_SEARCH, params={
            "resource_id": CRO_DATASET_ID, "q": query,
            "limit": page_size, "offset": offset,
        })
        if payload.get("success") is False:
            raise FetchError("CRO open-data API returned success=false")
        block = payload.get("result") or {}
        records = block.get("records", [])
        if not records:
            break
        for item in records:
            number = str(item.get("company_num") or "").strip()
            name = str(item.get("company_name") or "").strip()
            form = str(item.get("company_type") or item.get("company_type_code") or "").strip()
            form_key = form.casefold()
            if not number or not name or not any(term in form_key for term in commercial_types):
                continue
            status = str(item.get("company_status") or "").strip()
            results.append({
                "country": "IE", "registry_id": number, "legal_name": name,
                "legal_form": form, "legal_person": True,
                "registry_status": "active" if status.casefold() in {"normal", "registered", "existing"} else "inactive",
                "registry_source": "Ireland CRO Open Data",
                "raw": {"company_status": status, "company_type_code": item.get("company_type_code"),
                        "company_status_code": item.get("company_status_code"),
                        "company_registered_date": item.get("company_reg_date"),
                        "nace_v2_code": item.get("nace_v2_code")},
            })
            if len(results) >= limit:
                break
        offset += len(records)
        if len(records) < page_size or offset >= int(block.get("total") or offset):
            break
    return results


def _prh_companies(query: str, limit: int, http: Http) -> list[dict]:
    """Search the Finnish Trade Register's free daily-updated YTJ open-data API."""
    results, page = [], 1
    allowed_forms = {
        "oy", "oyj", "ay", "ky", "ok", "sce", "se",
        "osakeyhtiö", "julkinen osakeyhtiö", "avoin yhtiö", "kommandiittiyhtiö",
        "eurooppayhtiö", "osuuskunta", "aktiebolag", "publikt aktiebolag",
        "öppet bolag", "kommanditbolag", "andelslag",
    }
    while len(results) < limit:
        payload = http.get_json(PRH_SEARCH, params={"name": query, "page": page})
        companies = payload.get("companies", [])
        if not companies:
            break
        for item in companies:
            business_id = item.get("businessId") or {}
            number = _as_text(business_id.get("value") if isinstance(business_id, dict) else business_id)
            names = item.get("names") or []
            name = ""
            if isinstance(names, list):
                # Prefer a current name entry; fall back to the first name only
                # when the API omits version metadata.
                current_names = [entry for entry in names if isinstance(entry, dict)
                                 and (entry.get("version") == 1 or entry.get("endDate") in (None, ""))]
                for entry in current_names or names:
                    name = _as_text(entry.get("name") if isinstance(entry, dict) else entry)
                    if name:
                        break
            forms = item.get("companyForms") or []
            form_values = []
            for entry in forms if isinstance(forms, list) else []:
                if isinstance(entry, dict):
                    # Use the current form only; a previous legal form must not
                    # make an entity eligible after a change of legal status.
                    if entry.get("version") not in (None, 1) or entry.get("endDate") not in (None, ""):
                        continue
                    form_values.append(_as_text(entry.get("type")))
                    descriptions = entry.get("descriptions") or []
                    form_values.extend(_as_text(d.get("description") if isinstance(d, dict) else d) for d in descriptions)
                else:
                    form_values.append(_as_text(entry))
            form_values = [v for v in form_values if v]
            form = next((v for v in form_values if v.casefold() in allowed_forms),
                        next((v for v in form_values if any(a in v.casefold() for a in allowed_forms if len(a) > 3)), ""))
            if not number or not name or not form:
                continue
            trade_status = str(item.get("tradeRegisterStatus") or "").casefold()
            business_status = str(item.get("status") or "").casefold()
            situations = item.get("companySituations") or []
            situation_text = " ".join(_as_text(x.get("type") if isinstance(x, dict) else x) for x in situations).casefold()
            ended = bool(item.get("endDate")) or any(x in situation_text for x in ("bankrupt", "konkur", "likvid", "liquidat"))
            if ended:
                registry_status = "inactive"
            elif any(x in trade_status for x in ("registered", "entered", "in the trade register")):
                registry_status = "active"
            elif any(x in trade_status for x in ("pending", "processing", "application")):
                registry_status = "pending"
            else:
                # API also includes pending registrations. Unknown status codes
                # stay blocked until decoded rather than being treated as active.
                registry_status = "pending"
            website = _as_text(item.get("website")) or None
            results.append({
                "country": "FI", "registry_id": number, "legal_name": name,
                "legal_form": form, "legal_person": True,
                "registry_status": registry_status,
                "registry_source": "PRH/YTJ Open Data",
                "official_domain": website, "domain_verified": bool(website),
                "domain_method": "registry_registered_website" if website else None,
                "raw": {"company_forms": form_values, "company_situations": situations,
                        "trade_register_status": item.get("tradeRegisterStatus"),
                        "business_id_status": item.get("status"),
                        "last_modified": item.get("lastModified")},
            })
            if len(results) >= limit:
                break
        total = int(payload.get("totalResults") or 0)
        if len(companies) < 100 or (total and page * 100 >= total):
            break
        page += 1
    return results


def _brreg_companies(query: str, limit: int, http: Http) -> list[dict]:
    """Search Norway's public Enhetsregisteret API; sole proprietors are excluded."""
    results, page = [], 0
    allowed_forms = {"AS", "ASA", "ANS", "DA", "KS", "SA"}
    while len(results) < limit:
        page_size = min(100, limit - len(results))
        payload = http.get_json(BRREG_SEARCH, params={"navn": query, "size": page_size, "page": page})
        embedded = payload.get("_embedded") or {}
        items = embedded.get("enheter") or payload.get("enheter") or []
        if not items:
            break
        for item in items:
            number = str(item.get("organisasjonsnummer") or "").strip()
            name = str(item.get("navn") or "").strip()
            org_form = item.get("organisasjonsform") or {}
            form = str(org_form.get("kode") or "").upper()
            if not number or not name or form not in allowed_forms:
                continue
            status = "inactive" if (item.get("slettedato") or item.get("konkurs") or
                                  item.get("underAvvikling") or item.get("underTvangsavviklingEllerTvangsopplosning")) else "active"
            website = _as_text(item.get("hjemmeside")) or None
            results.append({
                "country": "NO", "registry_id": number, "legal_name": name,
                "legal_form": form, "legal_person": True,
                "registry_status": status, "registry_source": "Brønnøysundregistrene Enhetsregisteret",
                "official_domain": website, "domain_verified": bool(website),
                "domain_method": "registry_registered_website" if website else None,
                "raw": {"organisation_form_description": org_form.get("beskrivelse"),
                        "bankruptcy": item.get("konkurs"), "under_liquidation": item.get("underAvvikling"),
                        "registered_in_business_register": item.get("registrertIForetaksregisteret"),
                        "industry_code": (item.get("naeringskode1") or {}).get("kode")},
            })
            if len(results) >= limit:
                break
        page_meta = payload.get("page") or {}
        total_pages = page_meta.get("totalPages")
        if len(items) < page_size or (total_pages is not None and page + 1 >= int(total_pages)):
            break
        page += 1
    return results


def inspect_registry_file(path: Path, member_name: str | None = None) -> list[str]:
    """Inspect a CSV header before creating a country-specific field mapping."""
    with _open_registry_csv(path, member_name) as text:
        sample = text.read(8192)
        text.seek(0)
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=";,\t|").delimiter
        except csv.Error:
            delimiter = ";"
        reader = csv.DictReader(text, delimiter=delimiter)
        if not reader.fieldnames:
            raise ValueError("No CSV header found in the selected registry file")
        return [str(name).strip() for name in reader.fieldnames if name]


def import_mapped_registry_file(con: sqlite3.Connection, country: str, path: Path,
                                mapping_path: Path, source: str,
                                confirm_full_snapshot: bool = False) -> int:
    """Import a single-table CSV/ZIP with a strict legal-form allowlist.

    Used for registers that publish bulk snapshots rather than a free search API.
    It never guesses official field names or assumes all legal entities are commercial.
    """
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    required = {"number_column", "name_column", "status_column", "active_values",
                "legal_form_column", "allowed_legal_forms", "snapshot_date"}
    missing = required - set(mapping)
    if missing:
        raise ValueError(f"{country} mapping missing keys: {', '.join(sorted(missing))}")
    if not mapping.get("allowed_legal_forms"):
        raise ValueError("Set allowed_legal_forms after inspecting the official legal-form catalogue; non-commercial forms must stay excluded")
    if mapping.get("full_snapshot") is True and not confirm_full_snapshot:
        raise ValueError("A full snapshot import requires --confirm-full-snapshot")
    snapshot_date = _snapshot_date(mapping)
    active_values = {str(v).casefold() for v in mapping["active_values"]}
    inactive_values = {str(v).casefold() for v in mapping.get("inactive_values", [])}
    legal_forms = {str(v).casefold() for v in mapping["allowed_legal_forms"]}
    if mapping.get("full_snapshot") is True:
        con.execute("UPDATE companies SET registry_status='inactive', updated_at=? WHERE country=?", (now_iso(), country))
    count = 0
    for row in _read_registry_rows(path, mapping.get("member")):
        norm = {str(k).strip(): str(v or "").strip() for k, v in row.items() if k is not None}
        number = norm.get(mapping["number_column"], "")
        name = norm.get(mapping["name_column"], "")
        status = norm.get(mapping["status_column"], "")
        legal_form = norm.get(mapping["legal_form_column"], "")
        if not number or not name or legal_form.casefold() not in legal_forms:
            continue
        active, inactive = status.casefold() in active_values, status.casefold() in inactive_values
        if not (active or inactive):
            continue
        website = norm.get(mapping.get("website_column", ""), "") or None
        upsert_candidate(con, country=country, registry_id=number, legal_name=name,
                         legal_form=legal_form, legal_person=True,
                         registry_status="active" if active else "inactive", registry_source=source,
                         official_domain=website, domain_verified=bool(website),
                         domain_method="registry_registered_website" if website else None,
                         raw={"status": status, "legal_form": legal_form,
                              "activity_code": norm.get(mapping.get("activity_column", ""), "")},
                         registry_checked_at=snapshot_date, commit=False)
        count += 1
    con.commit()
    return count


def import_candidates(con: sqlite3.Connection, items: list[dict]) -> int:
    count = 0
    for item in items:
        upsert_candidate(con, **item, commit=False)
        count += 1
    con.commit()
    return count


@contextmanager
def _open_registry_csv(path: Path, member_name: str | None = None):
    """Stream CSV rows from plain, gzip or ZIP files without loading huge dumps in RAM.

    Official registry archives can contain several CSVs. Callers must name the
    intended member instead of accidentally importing code/contact rows as the
    company table.
    """
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            if member_name:
                matches = [n for n in names if Path(n).name.casefold() == Path(member_name).name.casefold()]
                if not matches:
                    raise ValueError(f"ZIP archive does not contain the expected member: {member_name}")
                selected = matches[0]
            else:
                csv_names = [n for n in names if n.lower().endswith((".csv", ".txt")) and "meta" not in n.lower()]
                if len(csv_names) != 1:
                    raise ValueError("ZIP contains multiple CSV files; specify the correct member in the mapping")
                selected = csv_names[0]
            with zf.open(selected) as binary:
                with io.TextIOWrapper(binary, encoding="utf-8-sig", errors="replace", newline="") as text:
                    yield text
    elif path.name.lower().endswith(".csv.gz") or path.suffix.lower() == ".gz":
        with gzip.open(path, mode="rt", encoding="utf-8-sig", errors="replace", newline="") as text:
            yield text
    else:
        with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as text:
            yield text


def _read_registry_rows(path: Path, member_name: str | None = None):
    with _open_registry_csv(path, member_name) as text:
        sample = text.read(8192)
        text.seek(0)
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=";,\t|").delimiter
        except csv.Error:
            delimiter = ";"
        yield from csv.DictReader(text, delimiter=delimiter)


def _read_cbe_meta(path: Path) -> dict[str, str]:
    if path.suffix.lower() != ".zip":
        return {}
    try:
        rows = _read_registry_rows(path, "meta.csv")
        return {str(r.get("Variable") or "").strip(): str(r.get("Value") or "").strip() for r in rows}
    except ValueError:
        return {}


def _parse_registry_date(value: str) -> str:
    value = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError(f"Could not parse official registry snapshot date: {value!r}")


def _cbe_legal_names(con: sqlite3.Connection, path: Path, mapping: dict) -> None:
    """Load only legal-name denominations into a temporary SQLite lookup table."""
    member = mapping.get("denomination_file", "denomination.csv")
    required = {"denomination_entity_column", "denomination_type_column", "denomination_value_column", "legal_name_type_values"}
    missing = required - set(mapping)
    if missing:
        raise ValueError(f"CBE denomination mapping missing keys: {', '.join(sorted(missing))}")
    con.execute("CREATE TEMP TABLE IF NOT EXISTS cbe_legal_names (entity_number TEXT PRIMARY KEY, legal_name TEXT NOT NULL, language_rank INTEGER NOT NULL)")
    con.execute("DELETE FROM cbe_legal_names")
    legal_types = {str(v).casefold() for v in mapping["legal_name_type_values"]}
    langs = [str(v).upper() for v in mapping.get("preferred_languages", ["EN", "FR", "NL", "DE"])]
    lang_rank = {language: index for index, language in enumerate(langs)}
    for row in _read_registry_rows(path, member):
        entity = str(row.get(mapping["denomination_entity_column"]) or "").strip().replace(".", "")
        typ = str(row.get(mapping["denomination_type_column"]) or "").strip().casefold()
        value = str(row.get(mapping["denomination_value_column"]) or "").strip()
        language = str(row.get(mapping.get("denomination_language_column", "Language")) or "").strip().upper()
        if not entity or not value or typ not in legal_types or language not in lang_rank:
            continue
        con.execute("""INSERT INTO cbe_legal_names(entity_number, legal_name, language_rank) VALUES(?,?,?)
          ON CONFLICT(entity_number) DO UPDATE SET legal_name=excluded.legal_name, language_rank=excluded.language_rank
          WHERE excluded.language_rank < cbe_legal_names.language_rank""", (entity, value, lang_rank[language]))
    if con.execute("SELECT COUNT(*) FROM cbe_legal_names").fetchone()[0] == 0:
        raise ValueError("No legal names matched the CBE denomination mapping. Check the TypeOfDenomination codes in code.csv.")


def _cbe_website_hints(con: sqlite3.Connection, path: Path, mapping: dict) -> None:
    """Load registry-listed websites as candidates; these are official-register evidence."""
    member = mapping.get("contact_file", "contact.csv")
    required = {"contact_entity_column", "contact_type_column", "contact_value_column", "website_contact_type_values"}
    missing = required - set(mapping)
    if missing:
        raise ValueError(f"CBE contact mapping missing keys: {', '.join(sorted(missing))}")
    con.execute("CREATE TEMP TABLE IF NOT EXISTS cbe_website_hints (entity_number TEXT PRIMARY KEY, website TEXT NOT NULL)")
    con.execute("DELETE FROM cbe_website_hints")
    website_types = {str(v).casefold() for v in mapping["website_contact_type_values"]}
    for row in _read_registry_rows(path, member):
        entity = str(row.get(mapping["contact_entity_column"]) or "").strip().replace(".", "")
        typ = str(row.get(mapping["contact_type_column"]) or "").strip().casefold()
        value = str(row.get(mapping["contact_value_column"]) or "").strip()
        if entity and value and typ in website_types and entity not in {"", "NULL"}:
            con.execute("INSERT OR IGNORE INTO cbe_website_hints(entity_number, website) VALUES(?,?)", (entity, value))


def inspect_cbe_codes(path: Path, categories: list[str] | None = None, language: str = "EN") -> list[dict]:
    """Read legal-form and field codes from the official CBE code.csv member."""
    if path.suffix.lower() != ".zip":
        raise ValueError("inspect-cbe expects the official CBE ZIP archive containing code.csv")
    wanted = {item.casefold() for item in (categories or ["JuridicalForm", "TypeOfDenomination", "ContactType"])}
    rows = []
    for row in _read_registry_rows(path, "code.csv"):
        category = str(row.get("Category") or "").strip()
        code = str(row.get("Code") or "").strip()
        row_language = str(row.get("Language") or "").strip().upper()
        description = str(row.get("Description") or "").strip()
        if category.casefold() in wanted and row_language == language.upper() and code:
            rows.append({"category": category, "code": code, "language": row_language, "description": description})
    if not rows:
        raise ValueError("No matching codes found in code.csv. Check the category names and requested language.")
    return rows


def _snapshot_date(mapping: dict, official_value: str | None = None) -> str:
    value = official_value or mapping.get("snapshot_date") or ""
    if not value:
        raise ValueError("Mapping must include snapshot_date in YYYY-MM-DD format, or the archive must contain meta.csv SnapshotDate")
    return _parse_registry_date(str(value))


def import_cbe_file(con: sqlite3.Connection, path: Path, mapping_path: Path,
                    confirm_full_snapshot: bool = False) -> int:
    """Import a complete official CBE Open Data archive or an explicitly mapped CSV.

    The official ZIP is relational: enterprise.csv contains identity/type/status,
    denomination.csv contains company names, and contact.csv may contain website
    contacts. We join by EnterpriseNumber/EntityNumber inside SQLite so large
    archives can be streamed without building a giant Python dictionary.
    """
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    meta = _read_cbe_meta(path)
    extract_type = (meta.get("ExtractType") or "").strip().casefold()
    if extract_type and "update" in extract_type:
        raise ValueError("CBE update archives are not supported yet; import the official Full snapshot instead")
    if mapping.get("full_snapshot") is True and not confirm_full_snapshot:
        raise ValueError("A full snapshot import requires --confirm-full-snapshot")
    if extract_type and "full" in extract_type and mapping.get("full_snapshot") is not True:
        raise ValueError("This archive is marked as a Full snapshot. Set full_snapshot=true and pass --confirm-full-snapshot")
    required = {"number_column", "status_column", "active_values", "legal_form_column", "legal_person_column", "legal_person_values"}
    missing = required - set(mapping)
    if missing:
        raise ValueError(f"CBE mapping missing keys: {', '.join(sorted(missing))}")
    snapshot_date = _snapshot_date(mapping, meta.get("SnapshotDate"))
    if path.suffix.lower() == ".zip" and not mapping.get("allowed_legal_forms"):
        raise ValueError("For CBE ZIP imports, set allowed_legal_forms after reviewing inspect-cbe output. This avoids importing associations and other non-commercial legal forms by default.")
    allowed_legal_forms = {str(value).strip().casefold() for value in mapping.get("allowed_legal_forms", [])}
    member = mapping.get("enterprise_file", "enterprise.csv")
    has_inline_name = bool(mapping.get("name_column"))
    has_joined_name = bool(mapping.get("denomination_file") or path.suffix.lower() == ".zip")
    if not has_inline_name and not has_joined_name:
        raise ValueError("CBE company names are in denomination.csv; provide a denomination_file mapping or an inline name_column")

    if path.suffix.lower() == ".zip" and not has_inline_name:
        _cbe_legal_names(con, path, mapping)
    if path.suffix.lower() == ".zip" and mapping.get("website_contact_type_values"):
        _cbe_website_hints(con, path, mapping)

    if mapping.get("full_snapshot") is True:
        con.execute("UPDATE companies SET registry_status='inactive', updated_at=? WHERE country='BE'", (now_iso(),))

    count = 0
    for row in _read_registry_rows(path, member if path.suffix.lower() == ".zip" else None):
        norm = {str(k).strip(): str(v or "").strip() for k, v in row.items() if k is not None}
        number = norm.get(mapping["number_column"], "").replace(".", "")
        name = norm.get(mapping["name_column"], "") if has_inline_name else ""
        if not name and not has_inline_name:
            found = con.execute("SELECT legal_name FROM cbe_legal_names WHERE entity_number=?", (number,)).fetchone()
            name = found["legal_name"] if found else ""
        status = norm.get(mapping["status_column"], "")
        legal_form = norm.get(mapping["legal_form_column"], "")
        if allowed_legal_forms and legal_form.casefold() not in allowed_legal_forms:
            continue
        legal_flag = norm.get(mapping["legal_person_column"], "").casefold()
        active = status.casefold() in {str(v).casefold() for v in mapping["active_values"]}
        inactive_values = {str(v).casefold() for v in mapping.get("inactive_values", [])}
        is_inactive = status.casefold() in inactive_values
        legal_person = legal_flag in {str(v).casefold() for v in mapping["legal_person_values"]}
        if not number or not name or not (active or is_inactive) or not legal_person:
            continue

        website = None
        if path.suffix.lower() == ".zip" and mapping.get("website_contact_type_values"):
            hint = con.execute("SELECT website FROM cbe_website_hints WHERE entity_number=?", (number,)).fetchone()
            website = hint["website"] if hint else None
        # A domain copied from the company record in an official register is
        # trusted as registry evidence; set_domain remains the manual fallback.
        from .company_security import is_https_url
        normalized_website = None
        if website:
            candidate_url = website if website.startswith("https://") else f"https://{website.removeprefix('http://')}"
            if is_https_url(candidate_url):
                normalized_website = candidate_url
        upsert_candidate(con, country="BE", registry_id=number, legal_name=name, legal_form=legal_form,
                         legal_person=True, registry_status="active" if active else "inactive", registry_source="CBE Open Data",
                         official_domain=normalized_website,
                         domain_verified=bool(normalized_website),
                         domain_method="registry_registered_website" if normalized_website else None,
                         raw={"enterprise_number": number, "status": status, "legal_form": legal_form,
                              "website_from_registry": bool(normalized_website)},
                         registry_checked_at=snapshot_date, commit=False)
        count += 1
    con.commit()
    return count


def _classify_fr_employer_range(value: str) -> str:
    """Classify SIRENE employee bands; missing data is never assumed to employ."""
    code = str(value or "").strip().upper()
    if code == "NN":
        return "non_employer"
    if code in {"00", "01", "02", "03", "11", "12", "21", "22", "31", "32", "41", "42", "51", "52", "53"}:
        return "employer"
    return "unknown"


def import_fr_stock_file(con: sqlite3.Connection, path: Path, mapping_path: Path,
                         confirm_full_snapshot: bool = False) -> int:
    """Import INSEE StockUniteLegale and classify employers from employee bands.

    The employee-band column must be explicitly mapped. SIRENE employee bands
    are statistical and may be N-2/N-3; unknown values remain unresolved.
    """
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    if mapping.get("full_snapshot") is True and not confirm_full_snapshot:
        raise ValueError("A full snapshot import requires --confirm-full-snapshot")
    required = {"number_column", "name_column", "status_column", "active_values", "legal_form_column", "legal_form_prefixes", "employee_range_column"}
    missing = required - set(mapping)
    if missing:
        raise ValueError(f"INSEE mapping missing keys: {', '.join(sorted(missing))}")
    snapshot_date = _snapshot_date(mapping)
    if mapping.get("full_snapshot") is True:
        con.execute("UPDATE companies SET registry_status='inactive', updated_at=? WHERE country='FR'", (now_iso(),))
    active_values = {str(v).casefold() for v in mapping["active_values"]}
    inactive_values = {str(v).casefold() for v in mapping.get("inactive_values", [])}
    legal_prefixes = tuple(str(v) for v in mapping["legal_form_prefixes"])
    excluded_diffusion = {str(v).casefold() for v in mapping.get("excluded_diffusion_values", ["P", "N"])}
    diffusion_column = mapping.get("diffusion_column")
    count = 0
    for row in _read_registry_rows(path):
        norm = {str(k).strip(): str(v or "").strip() for k, v in row.items() if k is not None}
        number = norm.get(mapping["number_column"], "")
        name = norm.get(mapping["name_column"], "")
        legal_form = norm.get(mapping["legal_form_column"], "")
        status = norm.get(mapping["status_column"], "")
        diffusion = norm.get(diffusion_column, "") if diffusion_column else ""
        if not number or not name or not legal_form.startswith(legal_prefixes) or diffusion.casefold() in excluded_diffusion:
            continue
        active = status.casefold() in active_values
        inactive = status.casefold() in inactive_values
        if not (active or inactive):
            continue
        employee_range = norm.get(mapping["employee_range_column"], "").strip().upper()
        employer_scope = _classify_fr_employer_range(employee_range)
        upsert_candidate(con, country="FR", registry_id=number, legal_name=name, legal_form=legal_form,
                         legal_person=True, registry_status="active" if active else "inactive", registry_source="INSEE SIRENE stock",
                         raw={"activity_code": norm.get(mapping.get("activity_column", ""), ""),
                              "employee_range": employee_range or None,
                              "employee_range_date": norm.get(mapping.get("employee_range_date_column", ""), "") or None,
                              "employer_scope": employer_scope,
                              "status": status, "diffusion_status": diffusion},
                         registry_checked_at=snapshot_date, commit=False)
        count += 1
    con.commit()
    return count


def approved_companies(db: Path = DEFAULT_DB) -> list[dict]:
    if not db.exists():
        return []
    con = connect(db)
    rows = con.execute("""SELECT c.*, s.ats, s.ats_slug, s.career_url, s.approval_method, s.last_checked, s.approved_at
      FROM companies c JOIN career_sources s ON s.company_id=c.id
      WHERE c.registry_status='active' AND c.legal_person=1 AND c.domain_verified=1 AND s.approved=1""").fetchall()
    out = []
    for row in rows:
        key = f"{row['country'].lower()}-{row['registry_id']}"
        out.append({
            "name": row["trading_name"] or row["legal_name"], "slug": key, "company_key": key,
            "ats": row["ats"], "ats_slug": row["ats_slug"], "hq": row["country"],
            "official_domain": row["official_domain"],
            "approved_hosts": [urlparse(row["career_url"]).hostname] if row["ats"] == "jsonld" else [],
            "urls": [row["career_url"]] if row["ats"] == "jsonld" else [],
            "verification": {
                "status": "approved",
                "registry": {"verified": True, "country": row["country"], "id": row["registry_id"],
                              "status": "active", "legal_person": True, "source": row["registry_source"],
                              "checked_at": row["registry_checked_at"]},
                "domain": {"url": f"https://{row['official_domain']}", "verified": True,
                           "method": row["domain_method"], "checked_at": row["domain_checked_at"]},
                "source": {"approved": True, "verified": True, "method": row["approval_method"],
                           "career_url": row["career_url"], "checked_at": row["last_checked"] or row["approved_at"]},
            },
        })
    con.close()
    return out


def export_approved(db: Path, output: Path) -> int:
    """Export a small, reviewed publication snapshot; keep the full candidate DB local."""
    from .company_security import is_company_approved
    companies = [c for c in approved_companies(db) if is_company_approved(c)[0]]
    payload = {"generated_at": now_iso(), "count": len(companies), "companies": companies}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(companies)


def discover_all_sources(con: sqlite3.Connection, http: Http, country: str | None = None,
                          limit: int = 100, only_without_sources: bool = True) -> tuple[int, int]:
    """Discover career sources for a bounded batch of verified company domains.

    By default, companies are due for discovery when never checked or when their
    last successful discovery is at least 90 days old. This includes companies
    with no career page, so they do not block later candidates. Failed attempts
    are retried after seven days. Registry and domain checks must also be fresh.
    Pass only_without_sources=False to force a batch re-check regardless of the
    previous successful discovery date or retry window.
    """
    query = """SELECT c.country,c.registry_id,c.legal_name FROM companies c
      WHERE c.registry_status='active' AND c.legal_person=1 AND c.domain_verified=1"""
    params: list[str | int] = []
    if country:
        query += " AND c.country=?"
        params.append(country.upper())
    if country and country.upper() == "FR":
        # France-first discovery is employer-only. Unknown headcount remains a
        # visible coverage gap, not permission to crawl the organisation.
        query += " AND json_extract(c.raw_json, '$.employer_scope')='employer'"
    # Do not spend batch slots on stale identity/domain checks. They need an
    # explicit registry/domain refresh first. Also use attempt backoff so an
    # unreachable or robots-disallowed site cannot stall the queue.
    query += " AND c.registry_checked_at IS NOT NULL AND substr(c.registry_checked_at,1,10) >= date('now','-90 day') AND substr(c.registry_checked_at,1,10) <= date('now','+1 day')"
    query += " AND c.domain_checked_at IS NOT NULL AND substr(c.domain_checked_at,1,10) >= date('now','-90 day') AND substr(c.domain_checked_at,1,10) <= date('now','+1 day')"
    if only_without_sources:
        query += " AND (c.career_discovery_checked_at IS NULL OR substr(c.career_discovery_checked_at,1,10) <= date('now','-90 day'))"
        query += " AND (c.career_discovery_attempted_at IS NULL OR substr(c.career_discovery_attempted_at,1,10) <= date('now','-7 day'))"
    query += " ORDER BY c.country,c.legal_name,c.registry_id LIMIT ?"
    params.append(max(1, min(int(limit), 5000)))
    rows = con.execute(query, params).fetchall()
    discovered = 0
    for row in rows:
        try:
            items = discover_company_sources(con, row["country"], row["registry_id"], http)
            discovered += len(items)
            state = "no career source found" if not items else f"{len(items)} source(s) discovered"
            print(f"{row['country']} {row['registry_id']} | {row['legal_name']} | {state}")
        except (ValueError, FetchError, OSError) as exc:
            # The attempt timestamp remains, while the successful-check date
            # stays unchanged. The seven-day backoff prevents queue starvation.
            con.execute("UPDATE companies SET career_discovery_status=? WHERE country=? AND registry_id=?",
                        (f"failed: {str(exc)[:180]}", row["country"], row["registry_id"]))
            print(f"{row['country']} {row['registry_id']} | {row['legal_name']} | ERROR: {exc}", file=sys.stderr)
    con.commit()
    return len(rows), discovered


def print_france_coverage(con: sqlite3.Connection) -> None:
    """Print the France employer funnel and unresolved coverage gaps."""
    active = con.execute("""SELECT COUNT(*) FROM companies
      WHERE country='FR' AND registry_status='active' AND legal_person=1""").fetchone()[0]
    bands = con.execute("""SELECT
      COALESCE(json_extract(raw_json, '$.employer_scope'), 'unknown') AS scope,
      COUNT(*) AS total
      FROM companies
      WHERE country='FR' AND registry_status='active' AND legal_person=1
      GROUP BY scope ORDER BY scope""").fetchall()
    employer = con.execute("""SELECT COUNT(*) FROM companies
      WHERE country='FR' AND registry_status='active' AND legal_person=1
      AND json_extract(raw_json, '$.employer_scope')='employer'""").fetchone()[0]
    domain = con.execute("""SELECT COUNT(*) FROM companies
      WHERE country='FR' AND registry_status='active' AND legal_person=1
      AND json_extract(raw_json, '$.employer_scope')='employer'
      AND domain_verified=1""").fetchone()[0]
    discovery = con.execute("""SELECT COALESCE(career_discovery_status,'not_checked') AS status, COUNT(*) AS total
      FROM companies WHERE country='FR' AND registry_status='active' AND legal_person=1
      AND json_extract(raw_json, '$.employer_scope')='employer'
      GROUP BY status ORDER BY status""").fetchall()
    sources = con.execute("""SELECT
      SUM(CASE WHEN s.approved=1 THEN 1 ELSE 0 END) AS approved,
      SUM(CASE WHEN s.id IS NOT NULL AND s.approved=0 THEN 1 ELSE 0 END) AS pending
      FROM companies c LEFT JOIN career_sources s ON s.company_id=c.id
      WHERE c.country='FR' AND c.registry_status='active' AND c.legal_person=1
      AND json_extract(c.raw_json, '$.employer_scope')='employer'""").fetchone()
    print("FRANCE EMPLOYER COVERAGE")
    print(f"Active legal-entity records indexed: {active}")
    for row in bands:
        print(f"  {row['scope']}: {row['total']}")
    print(f"Confirmed employers (SIRENE employee band): {employer}")
    print(f"Employers with verified official domains: {domain}")
    print("Career discovery status:")
    for row in discovery:
        print(f"  {row['status']}: {row['total']}")
    print(f"Career sources approved: {sources['approved'] or 0}")
    print(f"Career sources pending review: {sources['pending'] or 0}")
    unknown = next((row['total'] for row in bands if row['scope'] == 'unknown'), 0)
    print(f"Unresolved employee-band gaps: {unknown}")
    print("Note: SIRENE employee bands are dated statistical ranges, often N-2/N-3; "
          "unknown values are not counted as employers. Respect non-diffusible records.")


def print_pending(con: sqlite3.Connection) -> None:
    rows = con.execute("""SELECT c.country,c.registry_id,c.legal_name,c.registry_status,c.legal_person,
      c.official_domain,c.domain_verified,c.career_discovery_checked_at,c.career_discovery_attempted_at,
      c.career_discovery_status,s.ats,s.ats_slug,s.approved,s.last_status
      FROM companies c LEFT JOIN career_sources s ON s.company_id=c.id
      ORDER BY c.country,c.legal_name""").fetchall()
    for r in rows:
        print(f"{r['country']} {r['registry_id']} | {r['legal_name']} | registry={r['registry_status']} legal_person={bool(r['legal_person'])} domain={r['official_domain'] or '-'} domain_verified={bool(r['domain_verified'])} | discovery={r['career_discovery_status'] or 'not_checked'} | source={r['ats'] or '-'}:{r['ats_slug'] or '-'} approved={bool(r['approved'])}")
    companies = con.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    approved = con.execute("SELECT COUNT(*) FROM career_sources WHERE approved=1").fetchone()[0]
    print(f"\n{companies} company records; {approved} approved career sources. Only active, fresh legal persons with a verified domain and approved source are crawlable.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    gleif_search = sub.add_parser("search-gleif", help="Search the free public GLEIF LEI index (no API key or registration)")
    gleif_search.add_argument("--query", required=True, help="Legal-entity name")
    gleif_search.add_argument("--country", help="Optional ISO-2 country filter, e.g. BE or NL")
    gleif_search.add_argument("--limit", type=int, default=100)
    search = sub.add_parser("search", help="Find registered legal entities by a targeted search query")
    search.add_argument("--country", choices=["FR", "NL", "GB", "IE", "FI", "NO"], required=True)
    search.add_argument("--query", required=True, help="Company name; INSEE query syntax for FR")
    search.add_argument("--limit", type=int, default=100)
    inspect_cbe = sub.add_parser("inspect-cbe", help="Show official CBE code.csv values before configuring company-form/name/website allowlists")
    inspect_cbe.add_argument("--file", required=True, help="Official CBE Full ZIP archive")
    inspect_cbe.add_argument("--category", nargs="+", default=["JuridicalForm", "TypeOfDenomination", "ContactType"], help="One or more code.csv categories")
    inspect_cbe.add_argument("--language", choices=["EN", "FR", "NL", "DE"], default="FR")
    imp = sub.add_parser("import-cbe", help="Import an authorised CBE Open Data CSV/ZIP using an explicit field mapping")
    imp.add_argument("--file", required=True)
    imp.add_argument("--mapping", required=True)
    imp.add_argument("--confirm-full-snapshot", action="store_true", help="The mapping must also contain full_snapshot=true; mark previously indexed BE entities inactive before import")
    frimp = sub.add_parser("import-fr-stock", help="Import the official INSEE StockUniteLegale CSV/ZIP using an explicit field mapping")
    frimp.add_argument("--file", required=True)
    frimp.add_argument("--mapping", required=True)
    frimp.add_argument("--confirm-full-snapshot", action="store_true", help="The mapping must also contain full_snapshot=true; mark previously indexed FR entities inactive before import")
    inspect_file = sub.add_parser("inspect-registry-file", help="Show CSV headers in an official registry archive before mapping")
    inspect_file.add_argument("--file", required=True)
    inspect_file.add_argument("--member", help="CSV member inside a ZIP archive")
    eeimp = sub.add_parser("import-ee-open-data", help="Import an Estonia e-Business Register CSV/ZIP with a strict legal-form allowlist")
    eeimp.add_argument("--file", required=True)
    eeimp.add_argument("--mapping", required=True)
    eeimp.add_argument("--confirm-full-snapshot", action="store_true")
    domain = sub.add_parser("set-domain", help="Record a company website after a human verification check")
    domain.add_argument("--country", required=True)
    domain.add_argument("--registry-id", required=True)
    domain.add_argument("--domain", required=True)
    domain.add_argument("--confirm", action="store_true", help="Attest that the domain relationship has been checked")
    discover_cmd = sub.add_parser("discover-careers", help="Discover career pages linked from a verified company domain")
    discover_cmd.add_argument("--country", required=True)
    discover_cmd.add_argument("--registry-id", required=True)
    approve = sub.add_parser("approve-source", help="Approve a discovered career feed after reviewing its relationship to the company")
    approve.add_argument("--country", required=True)
    approve.add_argument("--registry-id", required=True)
    approve.add_argument("--ats", required=True)
    approve.add_argument("--slug", required=True)
    approve.add_argument("--career-url", required=True)
    approve.add_argument("--method", choices=["linked_from_verified_company_domain", "registry_registered_website", "employer_domain_challenge", "manual_documented_review"], required=True)
    batch = sub.add_parser("discover-all", help="Discover career pages for a bounded batch of active, domain-verified companies (90-day recheck cadence)")
    batch.add_argument("--country", choices=["BE", "EE", "FI", "FR", "GB", "IE", "NL", "NO"])
    batch.add_argument("--limit", type=int, default=100)
    batch.add_argument("--include-existing", action="store_true", help="Force a re-check even when a company was successfully checked within the last 90 days")
    export = sub.add_parser("export-approved", help="Export approved company sources to a small JSON snapshot for deployment")
    export.add_argument("--output", default="companies-approved.json")
    sub.add_parser("coverage-fr", help="Show the France-first employer discovery funnel and unresolved headcount gaps")
    sub.add_parser("pending", help="List company and source approval status")
    args = parser.parse_args(argv)
    db = Path(args.db)
    con = connect(db)
    try:
        if args.command == "init":
            print(f"Initialised registry database: {db}")
        elif args.command == "search-gleif":
            http = Http(min_delay=0.35)
            items = _gleif_companies(args.query, args.limit, http, args.country)
            count = import_candidates(con, items)
            print(f"Imported {count} GLEIF legal-entity candidates into {db}. No domain was inferred; candidates are NOT approved for crawling.")
        elif args.command == "search":
            http = Http(min_delay=2.1 if args.country == "FR" else 0.5)
            searchers = {
                "FR": lambda: _insee_companies(args.query, args.limit, http),
                "NL": lambda: _kvk_companies(args.query, args.limit, http),
                "GB": lambda: _companies_house_companies(args.query, args.limit, http),
                "IE": lambda: _cro_companies(args.query, args.limit, http),
                "FI": lambda: _prh_companies(args.query, args.limit, http),
                "NO": lambda: _brreg_companies(args.query, args.limit, http),
            }
            items = searchers[args.country]()
            count = import_candidates(con, items)
            print(f"Imported {count} registered-company candidates into {db}. Candidates are NOT yet approved for crawling.")
        elif args.command == "inspect-cbe":
            for item in inspect_cbe_codes(Path(args.file), args.category, args.language):
                print(f"{item['category']}\t{item['code']}\t{item['description']}")
        elif args.command == "import-cbe":
            mapping = json.loads(Path(args.mapping).read_text(encoding="utf-8"))
            if args.confirm_full_snapshot and not mapping.get("full_snapshot"):
                raise ValueError("Set full_snapshot=true in the mapping file only after confirming this is an official complete snapshot")
            count = import_cbe_file(con, Path(args.file), Path(args.mapping), args.confirm_full_snapshot)
            print(f"Imported {count} legal-entity candidates from the authorised CBE file. Candidates are NOT yet approved.")
        elif args.command == "import-fr-stock":
            mapping = json.loads(Path(args.mapping).read_text(encoding="utf-8"))
            if args.confirm_full_snapshot and not mapping.get("full_snapshot"):
                raise ValueError("Set full_snapshot=true in the mapping file only after confirming this is an official complete snapshot")
            count = import_fr_stock_file(con, Path(args.file), Path(args.mapping), args.confirm_full_snapshot)
            print(f"Imported {count} legal-entity candidates from the INSEE stock file. Candidates are NOT yet approved.")
        elif args.command == "inspect-registry-file":
            for header in inspect_registry_file(Path(args.file), args.member):
                print(header)
        elif args.command == "import-ee-open-data":
            mapping = json.loads(Path(args.mapping).read_text(encoding="utf-8"))
            if args.confirm_full_snapshot and not mapping.get("full_snapshot"):
                raise ValueError("Set full_snapshot=true in the mapping only after confirming this is a complete official snapshot")
            count = import_mapped_registry_file(con, "EE", Path(args.file), Path(args.mapping),
                                                "Estonia e-Business Register Open Data", args.confirm_full_snapshot)
            print(f"Imported {count} legal-entity candidates from Estonia's official open-data file. Candidates are NOT yet approved.")
        elif args.command == "set-domain":
            set_domain(con, args.country, args.registry_id, args.domain, args.confirm)
            print("Domain recorded. It remains unverified unless --confirm was supplied after checking the relationship.")
        elif args.command == "discover-careers":
            try:
                result = discover_company_sources(con, args.country, args.registry_id, Http())
            except (ValueError, FetchError, OSError) as exc:
                con.execute("UPDATE companies SET career_discovery_status=? WHERE country=? AND registry_id=?",
                            (f"failed: {str(exc)[:180]}", args.country.upper(), args.registry_id))
                con.commit()
                raise
            for item in result:
                state = "AUTO-APPROVED" if item.get("auto_approve") else "PENDING REVIEW"
                print(f"{state} {item['ats']}:{item['slug']} | career={item['career_url']} | evidence={item['evidence_url']}")
            print("Only ATS sources directly embedded or linked on the verified official company website are auto-approved. Custom career pages remain pending review.")
        elif args.command == "approve-source":
            approve_source(con, args.country, args.registry_id, args.ats, args.slug, args.career_url, args.method)
            print("Career source approved. Its vacancies can now enter the validation pipeline.")
        elif args.command == "discover-all":
            visited, sources = discover_all_sources(con, Http(min_delay=0.75), args.country, args.limit,
                                                    only_without_sources=not args.include_existing)
            print(f"Checked {visited} company websites; discovered {sources} career source candidates.")
        elif args.command == "export-approved":
            count = export_approved(db, Path(args.output))
            print(f"Exported {count} approved company sources to {args.output}.")
        elif args.command == "coverage-fr":
            print_france_coverage(con)
        elif args.command == "pending":
            print_pending(con)
    except (ValueError, FetchError, OSError, sqlite3.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
