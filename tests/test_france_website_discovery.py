"""Offline QA for France company website discovery and legal-ID verification."""
import json
from datetime import datetime, timezone

from crawler.company_registry import connect, upsert_candidate
from crawler.france_website_discovery import (
    SCHEMA,
    _canonical_host,
    extract_legal_sirens,
    name_keys,
    verify_pending_candidates,
)


class FakeResponse:
    def __init__(self, url, text, status_code=200, headers=None):
        self.url = url
        self.text = text
        self.status_code = status_code
        self.headers = headers or {}


class FakeHttp:
    def __init__(self, pages):
        self.pages = pages
        self.visited = []

    def allowed_by_robots(self, url):
        return True

    def get(self, url, **kwargs):
        self.visited.append(url)
        if url not in self.pages:
            raise AssertionError(f"Unexpected URL requested: {url}")
        value = self.pages[url]
        if isinstance(value, Exception):
            raise value
        return FakeResponse(url, value)


def add_company_and_candidate(db, *, siren="123456789", domain="candidate.fr",
                              candidate_sirens=None, status="pending_verification",
                              last_checked_at=None):
    con = connect(db)
    upsert_candidate(
        con,
        country="FR",
        registry_id=siren,
        legal_name="EXEMPLE INDUSTRIE SAS",
        legal_form="5710",
        registry_status="active",
        registry_source="INSEE SIRENE",
        legal_person=True,
        raw={"employer_scope": "employer", "employee_range": "11"},
    )
    con.executescript(SCHEMA)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    con.execute(
        """INSERT INTO france_website_candidates
           (candidate_domain,candidate_url,normalized_name,place_name,
            source_record_ids_json,candidate_registry_ids_json,matched_company_count,
            match_method,match_score,source_release,status,first_seen_at,last_seen_at,last_checked_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            domain, f"https://{domain}/", "exemple industrie", "EXEMPLE INDUSTRIE",
            json.dumps(["overture-place-1"]),
            json.dumps(candidate_sirens or [siren]),
            len(candidate_sirens or [siren]), "exact_legal_name", 100.0,
            "2026-11-18.0", status, stamp, stamp, last_checked_at,
        ),
    )
    con.commit()
    return con


def test_french_name_matching_handles_accents_and_legal_suffix():
    assert "electricite de france" in name_keys("Électricité de France S.A.")
    assert "exemple industrie" in name_keys("EXEMPLE INDUSTRIE SAS")


def test_candidate_host_normalization_rejects_directories_and_social_profiles():
    assert _canonical_host("https://www.exemple.fr/contact") == "exemple.fr"
    assert _canonical_host("https://fr.linkedin.com/company/exemple") is None
    assert _canonical_host("https://www.pagesjaunes.fr/pros/123") is None
    assert _canonical_host("https://127.0.0.1") is None


def test_extracts_only_legally_labelled_siren_and_siret():
    page = """
      <p>SIREN : 123 456 789</p>
      <p>Numéro SIRET : 123 456 789 00012</p>
      <p>Telephone : 987 654 321</p>
    """
    assert extract_legal_sirens(page) == {"123456789"}


def test_exact_siren_match_verifies_and_persists_company_domain(tmp_path):
    db = tmp_path / "france.sqlite3"
    con = add_company_and_candidate(db)
    http = FakeHttp({
        "https://candidate.fr/": '<a href="/mentions-legales">Mentions légales</a>',
        "https://candidate.fr/mentions-legales": "<p>SIREN : 123 456 789</p>",
    })

    report = verify_pending_candidates(con, http, limit=10)
    company = con.execute(
        "SELECT official_domain,domain_verified,domain_method FROM companies WHERE registry_id='123456789'"
    ).fetchone()
    candidate = con.execute(
        "SELECT status,verification_note FROM france_website_candidates"
    ).fetchone()

    assert report["checked"] == 1
    assert report["verified_by_siren"] == 1
    assert (company["official_domain"], company["domain_verified"], company["domain_method"]) == (
        "candidate.fr", 1, "siren_in_legal_notice"
    )
    assert candidate["status"] == "verified_siren_match"
    assert "123456789" in candidate["verification_note"]
    con.close()


def test_mismatched_legal_identifier_never_verifies_candidate_domain(tmp_path):
    db = tmp_path / "france.sqlite3"
    con = add_company_and_candidate(db)
    http = FakeHttp({
        "https://candidate.fr/": '<a href="/mentions-legales">Mentions légales</a>',
        "https://candidate.fr/mentions-legales": "<p>SIREN : 987 654 321</p>",
    })

    report = verify_pending_candidates(con, http, limit=10)
    company = con.execute(
        "SELECT official_domain,domain_verified FROM companies WHERE registry_id='123456789'"
    ).fetchone()
    candidate = con.execute(
        "SELECT status FROM france_website_candidates"
    ).fetchone()

    assert report["identifier_mismatches"] == 1
    assert company["official_domain"] is None
    assert company["domain_verified"] == 0
    assert candidate["status"] == "legal_identifier_mismatch"
    con.close()


def test_stale_legal_notice_proof_is_revoked_when_company_id_changes(tmp_path):
    db = tmp_path / "france.sqlite3"
    con = add_company_and_candidate(
        db, status="verified_siren_match", last_checked_at="2026-01-01T00:00:00+00:00"
    )
    con.execute(
        """UPDATE companies SET official_domain='candidate.fr',domain_verified=1,
           domain_method='siren_in_legal_notice',domain_checked_at='2026-01-01T00:00:00+00:00'
           WHERE registry_id='123456789'"""
    )
    http = FakeHttp({
        "https://candidate.fr/": '<a href="/mentions-legales">Mentions légales</a>',
        "https://candidate.fr/mentions-legales": "<p>SIREN : 987 654 321</p>",
    })

    verify_pending_candidates(con, http, limit=10)
    company = con.execute(
        "SELECT official_domain,domain_verified,domain_method FROM companies WHERE registry_id='123456789'"
    ).fetchone()
    assert company["official_domain"] == "candidate.fr"
    assert company["domain_verified"] == 0
    assert company["domain_method"] is None
    con.close()
