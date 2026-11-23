"""Discover and verify French employer websites from open business-place data.

Overture Maps is a discovery source only. A domain is promoted into the company
registry only when a same-company SIREN/SIRET is found in a first-party legal
notice. French legal-notice terms and paths are deliberately supported.
"""
from __future__ import annotations

import argparse
import html
import ipaddress
import json
import re
import unicodedata
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

from .company_registry import DEFAULT_DB, connect, now_iso
from .http import FetchError, Http

STAC_CATALOG = "https://stac.overturemaps.org/catalog.json"
OVERTURE_S3 = "s3://overturemaps-us-west-2/release/{release}/theme=places/type=place/*.parquet"
RELEASE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.\d+$")
BATCH_SIZE = 2500

# These are directories, social networks, marketplaces or registry pages, not
# the employer's own website. Subdomains are rejected as well.
NON_COMPANY_HOSTS = {
    "facebook.com", "instagram.com", "linkedin.com", "x.com", "twitter.com",
    "tiktok.com", "youtube.com", "youtu.be", "wikipedia.org",
    "pagesjaunes.fr", "societe.com", "pappers.fr", "verif.com",
    "annuaire-entreprises.data.gouv.fr", "infogreffe.fr", "kompass.com",
    "tripadvisor.com", "yelp.com", "ubereats.com", "deliveroo.fr",
    "just-eat.fr", "indeed.com", "glassdoor.com", "welcome-to-the-jungle.com",
}

LEGAL_LINK_TERMS = (
    "mentions legales", "mention legale", "informations legales",
    "information legale", "infos legales", "legal notice", "legal-notice",
    "impressum",
)
LEGAL_PATHS = (
    "/mentions-legales", "/mentions-legales/", "/mentions_legales",
    "/informations-legales", "/infos-legales", "/legal-notice", "/legal",
    "/impressum",
)
LEGAL_ID_RE = re.compile(r"(?<!\d)(\d{3}[\s.\-]?\d{3}[\s.\-]?\d{3}(?:[\s.\-]?\d{5})?)(?!\d)")
LEGAL_MARKER_RE = re.compile(r"\b(siren|siret|r\.?c\.?s\.?)\b", re.I)
LEGAL_SUFFIXES = {
    "sas", "sasu", "sarl", "eurl", "sa", "sasu", "sci", "sc", "snc",
    "scop", "scic", "selarl", "selas", "s elarl", "gie", "sarl",
}
GENERIC_NAMES = {
    "entreprise", "societe", "commerce", "magasin", "restaurant",
    "boulangerie", "pharmacie", "epicerie", "boutique", "hotel",
    "bar", "brasserie", "cafe", "supermarche",
}

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS france_website_candidates (
  id INTEGER PRIMARY KEY,
  candidate_domain TEXT NOT NULL,
  candidate_url TEXT NOT NULL,
  normalized_name TEXT NOT NULL,
  place_name TEXT NOT NULL,
  source_record_ids_json TEXT NOT NULL,
  candidate_registry_ids_json TEXT NOT NULL,
  matched_company_count INTEGER NOT NULL,
  match_method TEXT NOT NULL,
  match_score REAL NOT NULL,
  address TEXT,
  locality TEXT,
  postcode TEXT,
  region TEXT,
  confidence REAL,
  source_release TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending_verification',
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  last_checked_at TEXT,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  verification_note TEXT,
  verified_legal_notice_url TEXT,
  UNIQUE(candidate_domain, normalized_name)
);
CREATE INDEX IF NOT EXISTS idx_fr_website_pending
  ON france_website_candidates(status, last_checked_at, id);
CREATE TABLE IF NOT EXISTS france_website_discovery_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS france_website_verification_cursor (
  queue_name TEXT PRIMARY KEY,
  last_candidate_id INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
"""


def _unaccent(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    return "".join(ch for ch in value if not unicodedata.combining(ch))


def name_keys(value: str) -> set[str]:
    """Conservative exact-name keys, including a legal-form-free variant."""
    tokens = re.findall(r"[a-z0-9]+", _unaccent(value).casefold())
    if not tokens:
        return set()
    keys = {" ".join(tokens)}
    trimmed = list(tokens)
    # Legal forms may be written with or without punctuation (S.A., S.A.S.).
    legal_form_sequences = {
        ("s", "a"), ("s", "a", "s"), ("s", "a", "s", "u"),
        ("s", "a", "r", "l"), ("e", "u", "r", "l"),
        ("s", "c", "i"), ("s", "n", "c"),
    }
    changed = True
    while changed and trimmed:
        changed = False
        for suffix in sorted(legal_form_sequences, key=len, reverse=True):
            if tuple(trimmed[-len(suffix):]) == suffix:
                trimmed = trimmed[:-len(suffix)]
                changed = True
                break
        if not changed and trimmed[-1] in LEGAL_SUFFIXES:
            trimmed.pop()
            changed = True
        if not changed and trimmed[0] in LEGAL_SUFFIXES:
            trimmed.pop(0)
            changed = True
    if trimmed:
        keys.add(" ".join(trimmed))
    return {key for key in keys if len(key.replace(" ", "")) >= 3 and key not in GENERIC_NAMES}


def _canonical_host(value: str) -> str | None:
    try:
        parsed = urlparse(value if "://" in value else f"https://{value}")
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username or parsed.password or parsed.port not in (None, 80, 443):
            return None
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        try:
            ip = ipaddress.ip_address(host)
            if not ip.is_global:
                return None
            return None
        except ValueError:
            pass
        host = host.removeprefix("www.")
        if "." not in host or host in {"example.com", "example.org", "localhost"}:
            return None
        if any(host == blocked or host.endswith("." + blocked) for blocked in NON_COMPANY_HOSTS):
            return None
        return host
    except (ValueError, UnicodeError):
        return None


def _company_name_index(con) -> dict[str, dict[str, str]]:
    """Build an exact-name index from active French legal entities."""
    index: dict[str, dict[str, str]] = {}
    rows = con.execute(
        """SELECT id,registry_id,legal_name,trading_name,raw_json
           FROM companies
           WHERE country='FR' AND registry_status='active' AND legal_person=1
             AND json_extract(raw_json, '$.employer_scope')='employer'"""
    ).fetchall()
    for row in rows:
        names = [(row["legal_name"], "exact_legal_name"),
                 (row["trading_name"], "exact_trading_name")]
        try:
            raw = json.loads(row["raw_json"] or "{}")
        except (TypeError, ValueError):
            raw = {}
        aliases = raw.get("trading_names") or raw.get("name_aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        names.extend((alias, "exact_trading_name") for alias in aliases if isinstance(alias, str))
        for candidate_name, method in names:
            if not candidate_name:
                continue
            for key in name_keys(candidate_name):
                bucket = index.setdefault(key, {})
                bucket.setdefault(str(row["registry_id"]), method)
    return index


def _clean_candidate_url(value: str) -> tuple[str, str] | None:
    if not isinstance(value, str) or len(value) > 2048:
        return None
    host = _canonical_host(value)
    if not host:
        return None
    parsed = urlparse(value if "://" in value else f"https://{value}")
    # Store an HTTPS URL; the HTTP variant is not used for verification.
    path = parsed.path or "/"
    query = f"?{parsed.query}" if parsed.query else ""
    return f"https://{host}{path}{query}", host


def _upsert_candidate(con, *, place_id: str, place_name: str, website_url: str,
                      domain: str, normalized_name: str, registry_ids: list[str],
                      match_method: str, address: str | None, locality: str | None,
                      postcode: str | None, region: str | None, confidence: float | None,
                      release: str) -> None:
    now = now_iso()
    existing = con.execute(
        """SELECT id,source_record_ids_json,candidate_registry_ids_json,status,
                  confidence,match_score FROM france_website_candidates
           WHERE candidate_domain=? AND normalized_name=?""",
        (domain, normalized_name),
    ).fetchone()
    if existing:
        try:
            source_ids = json.loads(existing["source_record_ids_json"] or "[]")
        except (TypeError, ValueError):
            source_ids = []
        try:
            existing_ids = json.loads(existing["candidate_registry_ids_json"] or "[]")
        except (TypeError, ValueError):
            existing_ids = []
        if place_id not in source_ids:
            source_ids.append(place_id)
        merged_ids = sorted(set(existing_ids) | set(registry_ids))
        con.execute(
            """UPDATE france_website_candidates
               SET candidate_url=?, source_record_ids_json=?, candidate_registry_ids_json=?,
                   matched_company_count=?, last_seen_at=?, source_release=?,
                   confidence=CASE WHEN confidence IS NULL THEN ? WHEN ? IS NULL THEN confidence
                                   ELSE MAX(confidence, ?) END,
                   address=COALESCE(address, ?), locality=COALESCE(locality, ?),
                   postcode=COALESCE(postcode, ?), region=COALESCE(region, ?),
                   match_method=CASE WHEN match_method='exact_trading_name' THEN ? ELSE match_method END
               WHERE id=?""",
            (website_url, json.dumps(source_ids), json.dumps(merged_ids), len(merged_ids), now,
             release, confidence, confidence, confidence, address, locality, postcode,
             region, match_method, existing["id"]),
        )
        return
    con.execute(
        """INSERT INTO france_website_candidates
           (candidate_domain,candidate_url,normalized_name,place_name,source_record_ids_json,
            candidate_registry_ids_json,matched_company_count,match_method,match_score,
            address,locality,postcode,region,confidence,source_release,status,
            first_seen_at,last_seen_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (domain, website_url, normalized_name, place_name,
         json.dumps([place_id]), json.dumps(sorted(set(registry_ids))), len(set(registry_ids)),
         match_method, 100.0 if len(set(registry_ids)) == 1 else 75.0,
         address, locality, postcode, region, confidence, release,
         "pending_verification", now, now),
    )


def ingest_overture_release(con, release: str) -> dict:
    """Stream French Overture place records with website fields into the candidate DB.

    No fixed employer ceiling is applied. Only exact company/trading-name
    matches enter the database; all website URLs remain unverified candidates.
    """
    try:
        import duckdb
    except ImportError as exc:
        raise RuntimeError("Install the duckdb dependency to query Overture Places") from exc

    name_index = _company_name_index(con)
    if not name_index:
        raise RuntimeError("No active French legal entities are loaded in the company registry")

    ddb = duckdb.connect()
    scanned = matched_places = candidate_domains = invalid_urls = 0
    try:
        ddb.execute("INSTALL httpfs")
        ddb.execute("LOAD httpfs")
        ddb.execute("SET s3_region='us-west-2'")
        source = OVERTURE_S3.format(release=release)
        query = f"""
            SELECT id, names.primary AS place_name, brand.names.primary AS brand_name, websites,
                   addresses[1].freeform AS address,
                   addresses[1].locality AS locality,
                   addresses[1].postcode AS postcode,
                   addresses[1].region AS region,
                   confidence
            FROM read_parquet('{source}')
            WHERE addresses IS NOT NULL
              AND len(addresses) > 0
              AND addresses[1].country = 'FR'
              AND websites IS NOT NULL
              AND len(websites) > 0
        """
        cursor = ddb.execute(query)
        while True:
            batch = cursor.fetchmany(BATCH_SIZE)
            if not batch:
                break
            for place_id, place_name, brand_name, websites, address, locality, postcode, region, confidence in batch:
                scanned += 1
                matched = {}
                matched_name = None
                for source_name in (place_name, brand_name):
                    if not source_name:
                        continue
                    source_keys = name_keys(str(source_name))
                    for key in source_keys:
                        for registry_id, method in name_index.get(key, {}).items():
                            matched.setdefault(registry_id, method)
                            if matched_name is None:
                                matched_name = str(source_name)
                if not matched or not matched_name:
                    continue
                matched_places += 1
                best_method = "exact_legal_name" if "exact_legal_name" in matched.values() else "exact_trading_name"
                matched_keys = name_keys(matched_name)
                normalized = min(matched_keys, key=lambda key: (len(key), key))
                website_values = websites if isinstance(websites, (list, tuple)) else [websites]
                for website in website_values:
                    cleaned = _clean_candidate_url(str(website or ""))
                    if not cleaned:
                        invalid_urls += 1
                        continue
                    candidate_url, domain = cleaned
                    before = con.total_changes
                    _upsert_candidate(
                        con, place_id=str(place_id), place_name=str(place_name),
                        website_url=candidate_url, domain=domain,
                        normalized_name=normalized, registry_ids=list(matched),
                        match_method=best_method, address=address, locality=locality,
                        postcode=postcode, region=region, confidence=confidence,
                        release=release,
                    )
                    if con.total_changes > before:
                        candidate_domains += 1
            con.commit()
        con.execute(
            """INSERT INTO france_website_discovery_meta(key,value,updated_at)
               VALUES('overture_release',?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
            (release, now_iso()),
        )
        con.commit()
    finally:
        ddb.close()
    return {
        "release": release,
        "french_places_with_websites_scanned": scanned,
        "exact_name_matched_places": matched_places,
        "candidate_rows_written_or_updated": candidate_domains,
        "invalid_or_non_company_urls": invalid_urls,
        "registry_name_keys": len(name_index),
    }


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self.text: list[str] = []
        self._href: str | None = None
        self._anchor_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            self._href = dict(attrs).get("href")
            self._anchor_text = []

    def handle_data(self, data):
        self.text.append(data)
        if self._href is not None:
            self._anchor_text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._href is not None:
            self.links.append((self._href, " ".join(self._anchor_text).strip()))
            self._href = None
            self._anchor_text = []


def _plain_text(page: str) -> str:
    parser = _PageParser()
    try:
        parser.feed(page)
        return html.unescape(" ".join(parser.text))
    except Exception:
        return html.unescape(re.sub(r"<[^>]+>", " ", page))


def extract_legal_sirens(page: str) -> set[str]:
    """Extract SIREN identifiers only when they follow SIREN/SIRET/RCS labels."""
    text = re.sub(r"\s+", " ", _unaccent(_plain_text(page)).casefold())
    found: set[str] = set()
    for marker in LEGAL_MARKER_RE.finditer(text):
        segment = text[marker.end():marker.end() + 100]
        match = LEGAL_ID_RE.search(segment)
        if not match:
            continue
        digits = re.sub(r"\D", "", match.group(1))
        if len(digits) in {9, 14}:
            found.add(digits[:9])
    return found


def _same_site(host: str, root: str) -> bool:
    host = host.lower().removeprefix("www.")
    root = root.lower().removeprefix("www.")
    return host == root or host.endswith("." + root)


def _get_same_site(http: Http, url: str, root_host: str):
    """Fetch without following redirects outside the candidate company domain."""
    current = url
    for _ in range(6):
        if not http.allowed_by_robots(current):
            raise PermissionError("robots.txt disallows this URL")
        response = http.get(current, allow_redirects=False)
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("Location")
            if not location:
                raise FetchError("redirect did not include a Location header")
            target = urljoin(current, location)
            parsed = urlparse(target)
            if parsed.scheme != "https" or not parsed.hostname or not _same_site(parsed.hostname, root_host):
                raise ValueError("website redirects outside the discovered company domain")
            current = target
            continue
        if not 200 <= response.status_code < 300:
            raise FetchError(f"unexpected HTTP status {response.status_code}")
        final_host = urlparse(response.url).hostname or ""
        if not _same_site(final_host, root_host):
            raise ValueError("website response ended outside the discovered company domain")
        return response
    raise FetchError("too many redirects while checking company website")


def _legal_notice_urls(home_url: str, homepage: str) -> list[str]:
    root_host = urlparse(home_url).hostname or ""
    parser = _PageParser()
    parser.feed(homepage)
    urls = []
    for href, label in parser.links:
        full = urljoin(home_url, href)
        parsed = urlparse(full)
        text = _unaccent(f"{label} {parsed.path}").casefold()
        if (parsed.scheme == "https" and parsed.hostname and _same_site(parsed.hostname, root_host)
                and any(term in text for term in LEGAL_LINK_TERMS)):
            urls.append(full)
    origin = f"https://{root_host}"
    urls.extend(urljoin(origin, path) for path in LEGAL_PATHS)
    return list(dict.fromkeys(urls))[:12]


def _verify_one(http: Http, row) -> tuple[str, str, str | None, str | None]:
    domain = row["candidate_domain"]
    root_url = f"https://{domain}/"
    try:
        home = _get_same_site(http, root_url, domain)
    except PermissionError as exc:
        return "robots_disallowed", str(exc), None, None
    except (FetchError, OSError, ValueError) as exc:
        return "unreachable_retry", str(exc)[:300], None, None

    expected = set(json.loads(row["candidate_registry_ids_json"] or "[]"))
    urls = _legal_notice_urls(home.url, home.text)
    saw_legal_page = False
    other_ids: set[str] = set()
    for legal_url in urls:
        try:
            page = _get_same_site(http, legal_url, domain)
        except PermissionError:
            continue
        except (FetchError, OSError, ValueError):
            continue
        saw_legal_page = True
        sirens = extract_legal_sirens(page.text)
        if sirens & expected:
            matched = sorted(sirens & expected)[0]
            return "verified_siren_match", f"French legal notice contains matching SIREN/SIRET {matched}", matched, legal_url
        other_ids.update(sirens)
    if other_ids:
        return "legal_identifier_mismatch", (
            "Legal notice contains a different SIREN/SIRET: " + ", ".join(sorted(other_ids)[:5])
        ), None, None
    if saw_legal_page:
        return "needs_manual_review", "Legal notice was found but no matching SIREN/SIRET was detected", None, None
    return "legal_notice_not_found", "No accessible French legal notice was found on the candidate domain", None, None


def verify_pending_candidates(con, http: Http, *, limit: int = 1000) -> dict:
    """Round-robin verify website candidates; only exact legal-ID matches auto-verify."""
    limit = max(1, min(int(limit), 5000))
    con.executescript(SCHEMA)
    candidate_columns = {row["name"] for row in con.execute("PRAGMA table_info(france_website_candidates)")}
    if "verified_legal_notice_url" not in candidate_columns:
        con.execute("ALTER TABLE france_website_candidates ADD COLUMN verified_legal_notice_url TEXT")
        con.commit()
    state = con.execute(
        "SELECT last_candidate_id FROM france_website_verification_cursor WHERE queue_name='FR_WEBSITES'"
    ).fetchone()
    last_id = int(state["last_candidate_id"]) if state else 0
    due = """(
             (status IN ('pending_verification','unreachable_retry','robots_disallowed',
                         'legal_notice_not_found','needs_manual_review')
              AND (last_checked_at IS NULL OR date(substr(last_checked_at,1,10)) <= date('now','-30 day')))
             OR
             (status IN ('verified_siren_match','legal_identifier_mismatch',
                         'verified_alternative_domain_needs_review')
              AND last_checked_at IS NOT NULL
              AND date(substr(last_checked_at,1,10)) <= date('now','-90 day'))
           )"""
    rows = con.execute(
        f"""SELECT * FROM france_website_candidates WHERE {due} AND id>?
            ORDER BY id LIMIT ?""", (last_id, limit)
    ).fetchall()
    if len(rows) < limit and last_id:
        extra = con.execute(
            f"""SELECT * FROM france_website_candidates WHERE {due} AND id<=?
                ORDER BY id LIMIT ?""", (last_id, limit - len(rows))
        ).fetchall()
        rows = list(rows) + list(extra)

    checked = verified = review = mismatch = failed = 0
    cursor_id = last_id
    for row in rows:
        cursor_id = int(row["id"])
        previous_status = row["status"]
        status, note, matched_siren, verified_legal_notice_url = _verify_one(http, row)
        checked += 1
        if status == "verified_siren_match":
            verified += 1
            company = con.execute(
                """SELECT id,official_domain,domain_verified FROM companies
                   WHERE country='FR' AND registry_id=?""", (matched_siren,)
            ).fetchone()
            if company:
                current_domain = (company["official_domain"] or "").lower().removeprefix("www.")
                if not company["official_domain"] or not company["domain_verified"] or current_domain == row["candidate_domain"]:
                    con.execute(
                        """UPDATE companies SET official_domain=?,domain_verified=1,
                           domain_method='siren_in_legal_notice',domain_checked_at=?,updated_at=?
                           WHERE id=?""",
                        (row["candidate_domain"], now_iso(), now_iso(), company["id"]),
                    )
                else:
                    status = "verified_alternative_domain_needs_review"
                    note += "; a different verified company domain is already recorded"
                    review += 1
            else:
                status = "verified_registry_id_not_loaded"
                note += "; SIREN matched the candidate evidence but no company row exists"
                review += 1
        elif status == "legal_identifier_mismatch":
            mismatch += 1
        elif status == "needs_manual_review":
            review += 1
        else:
            failed += 1

        # Re-check previously approved domains every 90 days. If the legal
        # notice no longer proves the same company, revoke this automated
        # verification and invalidate career-source approvals tied to it.
        if previous_status == "verified_siren_match" and status != "verified_siren_match":
            old_company = con.execute(
                """SELECT id FROM companies
                   WHERE country='FR' AND official_domain=? AND domain_method='siren_in_legal_notice'""",
                (row["candidate_domain"],),
            ).fetchall()
            for old in old_company:
                con.execute(
                    """UPDATE companies SET domain_verified=0,domain_method=NULL,
                       domain_checked_at=NULL,career_discovery_checked_at=NULL,
                       career_discovery_attempted_at=NULL,career_discovery_status='domain_verification_lost',
                       updated_at=? WHERE id=?""",
                    (now_iso(), old["id"]),
                )
                con.execute(
                    """UPDATE career_sources SET approved=0,approval_method=NULL,approved_at=NULL,
                       last_checked=?,last_status='company_domain_verification_lost'
                       WHERE company_id=?""",
                    (now_iso(), old["id"]),
                )

        con.execute(
            """UPDATE france_website_candidates
               SET status=?,last_checked_at=?,attempt_count=attempt_count+1,verification_note=?,
                   verified_legal_notice_url=?
               WHERE id=?""",
            (status, now_iso(), note, verified_legal_notice_url, row["id"]),
        )
        if checked % 25 == 0:
            con.commit()

    if rows:
        con.execute(
            """INSERT INTO france_website_verification_cursor(queue_name,last_candidate_id,updated_at)
               VALUES('FR_WEBSITES',?,?)
               ON CONFLICT(queue_name) DO UPDATE SET
               last_candidate_id=excluded.last_candidate_id,updated_at=excluded.updated_at""",
            (cursor_id, now_iso()),
        )
    con.commit()
    return {
        "checked": checked, "verified_by_siren": verified,
        "needs_review": review, "identifier_mismatches": mismatch,
        "unreachable_or_not_found": failed, "cursor_last_candidate_id": cursor_id,
    }


def build_report(con, release: str, ingest: dict | None, verification: dict) -> dict:
    status_rows = con.execute(
        "SELECT status,COUNT(*) AS count FROM france_website_candidates GROUP BY status ORDER BY status"
    ).fetchall()
    statuses = {row["status"]: row["count"] for row in status_rows}
    domains = con.execute(
        "SELECT COUNT(DISTINCT official_domain) FROM companies WHERE country='FR' AND domain_verified=1"
    ).fetchone()[0]
    companies = con.execute(
        "SELECT COUNT(*) FROM companies WHERE country='FR' AND registry_status='active' AND legal_person=1"
    ).fetchone()[0]
    return {
        "country": "FR",
        "source": "Overture Maps Places (candidate discovery only)",
        "source_release": release,
        "source_license": "CDLA Permissive 2.0 / Apache 2.0 (see Overture attribution requirements)",
        "generated_at": now_iso(),
        "active_legal_entities_in_registry": companies,
        "verified_official_domains_in_company_registry": domains,
        "website_candidate_rows": sum(statuses.values()),
        "candidate_status_counts": statuses,
        "latest_ingest": ingest or {"skipped": True, "reason": "Overture release already present in persistent SQLite cache"},
        "verification_batch": verification,
        "verification_rule": "A domain is automatically verified only when its same-company SIREN/SIRET is found in an accessible first-party legal notice. Other cases remain pending or need review.",
        "coverage_limit": "Overture covers public physical places, not every legal entity or online-only employer. Its Places dataset has documented duplicates and incomplete fields; results are candidates, not an exhaustive company directory.",
    }


def export_verified_websites(con, output: Path) -> int:
    """Publish only domains independently matched to a company's SIREN/SIRET."""
    rows = con.execute(
        """SELECT c.registry_id,c.legal_name,c.trading_name,c.official_domain,
                  c.domain_checked_at,c.registry_source,c.raw_json,
                  w.source_record_ids_json,w.source_release,w.address,w.locality,
                  w.postcode,w.region,w.verification_note,w.verified_legal_notice_url
           FROM companies c
           JOIN france_website_candidates w
             ON w.candidate_domain=c.official_domain
            AND w.status='verified_siren_match'
            AND json_valid(w.candidate_registry_ids_json)
            AND EXISTS (
              SELECT 1 FROM json_each(w.candidate_registry_ids_json) j
              WHERE CAST(j.value AS TEXT)=c.registry_id
            )
           WHERE c.country='FR' AND c.registry_status='active'
             AND c.legal_person=1 AND c.domain_verified=1
             AND c.domain_method='siren_in_legal_notice'
           ORDER BY COALESCE(c.trading_name,c.legal_name),c.registry_id"""
    ).fetchall()
    companies = []
    seen = set()
    for row in rows:
        if row["registry_id"] in seen:
            continue
        seen.add(row["registry_id"])
        try:
            raw = json.loads(row["raw_json"] or "{}")
        except (TypeError, ValueError):
            raw = {}
        companies.append({
            "name": row["trading_name"] or row["legal_name"],
            "legal_name": row["legal_name"],
            "siren": row["registry_id"],
            "country": "FR",
            "official_website": f"https://{row['official_domain']}/",
            "registry_record": f"https://annuaire-entreprises.data.gouv.fr/entreprise/{row['registry_id']}",
            "head_office_address": raw.get("head_office_address") or row["address"],
            "city": row["locality"],
            "postcode": row["postcode"],
            "region": row["region"],
            "activity_code": raw.get("activity_code"),
            "employee_range": raw.get("employee_range"),
            "verified_at": row["domain_checked_at"],
            "verification_method": "Matching SIREN/SIRET found in first-party French legal notice",
            "evidence": {
                "discovery_source": "Overture Maps Places",
                "source_release": row["source_release"],
                "place_record_ids": json.loads(row["source_record_ids_json"] or "[]"),
                "legal_notice_result": row["verification_note"],
                "legal_notice_url": row["verified_legal_notice_url"],
            },
        })
    payload = {
        "country": "FR",
        "generated_at": now_iso(),
        "count": len(companies),
        "verification_standard": "Only an exact same-company SIREN/SIRET match in an accessible first-party legal notice is included.",
        "companies": companies,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(companies)


def run(db: Path, *, verify_limit: int = 1000, report_path: Path | None = None,
        websites_output_path: Path | None = None, force_import: bool = False) -> dict:
    http = Http(min_delay=0.5, timeout=25, retries=2)
    catalog = http.get_json(STAC_CATALOG, headers={"Accept": "application/json"})
    release = str(catalog.get("latest") or "")
    if not RELEASE_RE.fullmatch(release):
        raise RuntimeError(f"Overture STAC returned an invalid latest release: {release!r}")

    con = connect(db)
    con.executescript(SCHEMA)
    previous = con.execute(
        "SELECT value FROM france_website_discovery_meta WHERE key='overture_release'"
    ).fetchone()
    ingest = None
    try:
        if force_import or not previous or previous["value"] != release:
            ingest = ingest_overture_release(con, release)
        else:
            print(f"Overture release {release} already imported; skipping full re-scan.")
        verification = verify_pending_candidates(
            con, Http(min_delay=0.5, timeout=20, retries=1), limit=verify_limit
        )
        verified_websites = export_verified_websites(
            con, websites_output_path
        ) if websites_output_path else 0
        report = build_report(con, release, ingest, verification)
        report["verified_websites_exported"] = verified_websites
        if report_path:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return report
    finally:
        con.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--verify-limit", type=int, default=1000,
                        help="Rolling verification batch size; cursor rotates through all candidates")
    parser.add_argument("--report", default="site/data/france-website-discovery-report.json")
    parser.add_argument("--websites-output", default="site/data/france-company-websites.json")
    parser.add_argument("--force-import", action="store_true")
    args = parser.parse_args(argv)
    run(Path(args.db), verify_limit=args.verify_limit,
        report_path=Path(args.report), websites_output_path=Path(args.websites_output),
        force_import=args.force_import)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
