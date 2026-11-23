"""Offline QA for France company website discovery and legal-ID verification."""
import json
from datetime import datetime, timezone

from crawler.company_registry import connect, upsert_candidate
from crawler.france_establishment_aliases import SCHEMA as ESTABLISHMENT_ALIAS_SCHEMA
from crawler.france_website_discovery import (
    SCHEMA,
    _canonical_host,
    _company_name_index,
    _upsert_candidate,
    extract_legal_sirens,
    export_verified_websites,
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
            return FakeResponse(url, "", status_code=404)
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
    assert "maison du sport" in name_keys("MAISON DU SPORT Société par actions simplifiée")


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

def test_public_export_contains_only_verified_same_company_websites(tmp_path):
    db = tmp_path / "france.sqlite3"
    con = add_company_and_candidate(db)
    http = FakeHttp({
        "https://candidate.fr/": '<a href="/mentions-legales">Mentions légales</a>',
        "https://candidate.fr/mentions-legales": "<p>SIRET : 123 456 789 00012</p>",
    })
    verify_pending_candidates(con, http, limit=10)
    output = tmp_path / "france-company-websites.json"
    count = export_verified_websites(con, output)
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert count == 1
    assert payload["count"] == 1
    assert payload["companies"][0]["siren"] == "123456789"
    assert payload["companies"][0]["official_website"] == "https://candidate.fr/"
    assert payload["companies"][0]["verification_method"].startswith("Matching SIREN/SIRET")
    assert payload["companies"][0]["evidence"]["legal_notice_url"] == "https://candidate.fr/mentions-legales"
    stored_proof = con.execute(
        "SELECT verified_legal_notice_url FROM france_website_candidates"
    ).fetchone()
    assert stored_proof["verified_legal_notice_url"] == "https://candidate.fr/mentions-legales"
    con.close()

def test_sirene_establishment_alias_links_shop_name_to_legal_employer(tmp_path):
    db = tmp_path / "france.sqlite3"
    con = add_company_and_candidate(db)
    con.executescript(ESTABLISHMENT_ALIAS_SCHEMA)
    con.execute(
        """INSERT INTO france_company_name_aliases
           (siren,alias,normalized_alias,source_siret,postcode,locality,source_field,source_release)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            "123456789", "MAISON DU SPORT", "maison du sport",
            "12345678900012", "75001", "PARIS",
            "enseigne1Etablissement", "2026-11-01",
        ),
    )
    index = _company_name_index(con)

    assert index["maison du sport"]["123456789"] == "exact_establishment_alias"
    con.close()

def test_new_registry_name_match_requeues_previously_mismatched_candidate(tmp_path):
    db = tmp_path / "france.sqlite3"
    con = add_company_and_candidate(
        db, candidate_sirens=["111111111"], status="legal_identifier_mismatch",
        last_checked_at="2026-11-20T00:00:00+00:00"
    )
    candidate = con.execute("SELECT * FROM france_website_candidates").fetchone()

    _upsert_candidate(
        con,
        place_id="overture-place-2",
        place_name="EXEMPLE INDUSTRIE",
        website_url="https://candidate.fr/",
        domain="candidate.fr",
        normalized_name="exemple industrie",
        registry_ids=["123456789"],
        match_method="exact_establishment_alias",
        address="1 Rue Exemple",
        locality="Paris",
        postcode="75001",
        region="Île-de-France",
        confidence=0.9,
        release="2026-11-18.0",
    )
    con.commit()
    refreshed = con.execute("SELECT * FROM france_website_candidates").fetchone()

    assert set(json.loads(refreshed["candidate_registry_ids_json"])) == {
        "111111111", "123456789"
    }
    assert refreshed["status"] == "pending_verification"
    assert refreshed["last_checked_at"] is None
    assert refreshed["verification_note"] is None
    con.close()
