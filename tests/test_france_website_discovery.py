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
    extract_overture_name_variants,
    extract_overture_source_evidence,
    export_verified_websites,
    ingest_wikidata_siren_websites,
    ingest_french_tech_next40_120,
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

    def get_text(self, url, **kwargs):
        response = self.get(url, **kwargs)
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code}: {url}")
        return response.text


class FakeJsonHttp:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get_json(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.payload


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
        "SELECT status,verification_note,verified_registry_id FROM france_website_candidates"
    ).fetchone()

    assert report["checked"] == 1
    assert report["verified_by_siren"] == 1
    assert (company["official_domain"], company["domain_verified"], company["domain_method"]) == (
        "candidate.fr", 1, "siren_in_legal_notice"
    )
    assert candidate["status"] == "verified_siren_match"
    assert candidate["verified_registry_id"] == "123456789"
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
        "SELECT status,verified_registry_id FROM france_website_candidates"
    ).fetchone()

    assert report["verified_by_siren"] == 0
    assert company["official_domain"] is None
    assert company["domain_verified"] == 0
    assert candidate["status"] == "verified_registry_id_not_loaded"
    assert candidate["verified_registry_id"] is None
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
    candidate = con.execute("SELECT verified_registry_id FROM france_website_candidates").fetchone()
    company = con.execute(
        "SELECT official_domain,domain_verified,domain_method FROM companies WHERE registry_id='123456789'"
    ).fetchone()
    assert company["official_domain"] == "candidate.fr"
    assert company["domain_verified"] == 0
    assert company["domain_method"] is None
    assert candidate["verified_registry_id"] is None
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
    assert payload["companies"][0]["verified_registry_id"] == "123456789"
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


def test_duckdb_lambda_syntax_used_by_overture_query_is_supported():
    import duckdb

    rows = duckdb.sql(
        "SELECT list_filter("
        "[struct_pack(country := 'FR'), struct_pack(country := 'BE')], "
        "lambda a : a.country = 'FR')"
    ).fetchall()[0][0]
    assert len(rows) == 1
    assert rows[0]["country"] == "FR"


def test_overture_name_variants_keep_french_common_and_alternate_names():
    names = {
        "primary": "Example Group",
        "common": {
            "fr": ["Groupe Exemple", "Exemple France"],
            "en": ["Example France"],
        },
        "rules": [
            {"value": "Groupe Exemple SAS", "language": "fr", "variant": "official"},
            {"value": "Example", "language": "en", "variant": "alternate"},
        ],
    }
    variants = extract_overture_name_variants(json.dumps(names))
    values = {(item["value"], item["language"], item["variant"]) for item in variants}
    assert ("Groupe Exemple", "fr", "common") in values
    assert ("Exemple France", "fr", "common") in values
    assert ("Groupe Exemple SAS", "fr", "official") in values
    assert ("Example France", "en", "common") in values


def test_overture_provenance_extraction_preserves_provider_and_license():
    evidence = extract_overture_source_evidence(json.dumps([
        {"property": "websites", "dataset": "places", "license": "ODbL",
         "record_id": "place-123", "provider": "Example Provider"},
        {"provider": "Second Provider", "version": "v2"},
    ]))
    assert evidence[0]["provider"] == "Example Provider"
    assert evidence[0]["record_id"] == "place-123"
    assert evidence[1]["version"] == "v2"


def test_wikidata_siren_link_adds_candidate_but_does_not_approve_domain(tmp_path):
    db = tmp_path / "france.sqlite3"
    con = connect(db)
    upsert_candidate(
        con,
        country="FR",
        registry_id="123456789",
        legal_name="EXEMPLE INDUSTRIE SAS",
        legal_form="5710",
        registry_status="active",
        registry_source="INSEE SIRENE",
        legal_person=True,
        raw={"employer_scope": "employer", "employee_range": "11"},
    )
    payload = {
        "results": {
            "bindings": [
                {
                    "company": {"value": "http://www.wikidata.org/entity/Q123"},
                    "siren": {"value": "123456789"},
                    "website": {"value": "https://candidate.fr"},
                    "label": {"value": "Exemple Industrie"},
                },
                {
                    "company": {"value": "http://www.wikidata.org/entity/Q999"},
                    "siren": {"value": "999999999"},
                    "website": {"value": "https://not-in-registry.fr"},
                    "label": {"value": "Unknown Company"},
                },
            ]
        }
    }
    http = FakeJsonHttp(payload)
    report = ingest_wikidata_siren_websites(con, http, pages=1, page_size=25)
    candidate = con.execute(
        "SELECT candidate_domain,status,match_method,source_evidence_json,matched_names_json "
        "FROM france_website_candidates"
    ).fetchone()
    company = con.execute(
        "SELECT official_domain,domain_verified FROM companies WHERE registry_id='123456789'"
    ).fetchone()

    assert report["wikidata_rows"] == 2
    assert report["matched_french_employers"] == 1
    assert report["unmatched_siren_rows"] == 1
    assert candidate["candidate_domain"] == "candidate.fr"
    assert candidate["status"] == "pending_verification"
    assert candidate["match_method"] == "wikidata_siren_property"
    assert json.loads(candidate["source_evidence_json"])[0]["website_property"] == "P856 (official website)"
    assert json.loads(candidate["matched_names_json"])[0]["language"] == "fr"
    assert company["official_domain"] is None
    assert company["domain_verified"] == 0
    assert len(http.calls) == 1
    assert "wdt:P1616" in http.calls[0][1]["params"]["query"]
    con.close()


def test_unmatched_place_name_can_be_verified_by_unique_siren_in_legal_notice(tmp_path):
    db = tmp_path / "france.sqlite3"
    con = connect(db)
    upsert_candidate(
        con,
        country="FR",
        registry_id="123456789",
        legal_name="EXEMPLE INDUSTRIE SAS",
        legal_form="5710",
        registry_status="active",
        registry_source="INSEE SIRENE",
        legal_person=True,
        raw={"employer_scope": "employer", "employee_range": "11"},
    )
    from crawler.france_website_discovery import ensure_discovery_schema
    ensure_discovery_schema(con)
    _upsert_candidate(
        con,
        place_id="overture-place-unmatched",
        place_name="Maison du Sport",
        website_url="https://candidate.fr/",
        domain="candidate.fr",
        normalized_name="maison du sport",
        registry_ids=[],
        match_method="unmatched_place_name",
        address="1 Rue Exemple",
        locality="Paris",
        postcode="75001",
        region="Île-de-France",
        confidence=0.95,
        release="2026-11-18.0",
        observed_names=[{
            "value": "Maison du Sport",
            "language": "fr",
            "variant": "primary",
            "source_field": "place.names.primary",
        }],
        source_evidence=[{
            "provider": "Overture",
            "record_id": "overture-place-unmatched",
            "license": "CDLA-Permissive-2.0",
        }],
    )
    http = FakeHttp({
        "https://candidate.fr/": '<a href="/mentions-legales">Mentions légales</a>',
        "https://candidate.fr/mentions-legales": "<p>SIREN : 123 456 789</p>",
    })

    report = verify_pending_candidates(con, http, limit=10)
    company = con.execute(
        "SELECT official_domain,domain_verified FROM companies WHERE registry_id='123456789'"
    ).fetchone()
    candidate = con.execute(
        "SELECT status,candidate_registry_ids_json,matched_company_count,observed_names_json,verified_registry_id "
        "FROM france_website_candidates"
    ).fetchone()

    assert report["verified_by_siren"] == 1
    assert company["official_domain"] == "candidate.fr"
    assert company["domain_verified"] == 1
    assert candidate["status"] == "verified_siren_match"
    assert candidate["verified_registry_id"] == "123456789"
    assert json.loads(candidate["candidate_registry_ids_json"]) == ["123456789"]
    assert candidate["matched_company_count"] == 1
    assert json.loads(candidate["observed_names_json"])[0]["value"] == "Maison du Sport"
    con.close()


def test_upsert_keeps_unmatched_observed_names_when_registry_match_arrives(tmp_path):
    db = tmp_path / "france.sqlite3"
    con = connect(db)
    from crawler.france_website_discovery import ensure_discovery_schema
    ensure_discovery_schema(con)
    observed = [{"value": "Maison du Sport", "language": "fr", "variant": "primary"}]
    _upsert_candidate(
        con, place_id="overture-1", place_name="Maison du Sport",
        website_url="https://candidate.fr/", domain="candidate.fr",
        normalized_name="maison du sport", registry_ids=[],
        match_method="unmatched_place_name", address=None, locality=None,
        postcode=None, region=None, confidence=0.9, release="2026-11-18.0",
        observed_names=observed,
    )
    _upsert_candidate(
        con, place_id="overture-2", place_name="Maison du Sport",
        website_url="https://candidate.fr/", domain="candidate.fr",
        normalized_name="maison du sport", registry_ids=["123456789"],
        match_method="exact_establishment_alias", address=None, locality=None,
        postcode=None, region=None, confidence=0.95, release="2026-11-18.0",
        matched_names=[{"value": "Maison du Sport", "registry_id": "123456789"}],
        observed_names=observed,
    )
    row = con.execute(
        "SELECT candidate_registry_ids_json,observed_names_json,matched_names_json,match_score "
        "FROM france_website_candidates"
    ).fetchone()
    assert json.loads(row["candidate_registry_ids_json"]) == ["123456789"]
    assert json.loads(row["observed_names_json"])[0]["value"] == "Maison du Sport"
    assert json.loads(row["matched_names_json"])[0]["registry_id"] == "123456789"
    con.close()


def test_one_domain_check_is_reused_for_duplicate_place_records(tmp_path):
    db = tmp_path / "france.sqlite3"
    con = add_company_and_candidate(db)
    _upsert_candidate(
        con,
        place_id="overture-place-duplicate",
        place_name="Autre enseigne",
        website_url="https://candidate.fr/",
        domain="candidate.fr",
        normalized_name="autre enseigne",
        registry_ids=["123456789"],
        match_method="exact_establishment_alias",
        address="1 Rue Exemple",
        locality="Paris",
        postcode="75001",
        region="Île-de-France",
        confidence=0.9,
        release="2026-11-18.0",
        observed_names=[{"value": "Autre enseigne", "language": "fr"}],
    )
    con.commit()
    http = FakeHttp({
        "https://candidate.fr/": '<a href="/mentions-legales">Mentions légales</a>',
        "https://candidate.fr/mentions-legales": "<p>SIREN : 123 456 789</p>",
    })

    report = verify_pending_candidates(con, http, limit=10)
    candidates = con.execute(
        "SELECT status FROM france_website_candidates ORDER BY id"
    ).fetchall()

    assert report["checked"] == 1
    assert report["unique_domains_checked"] == 1
    assert len(http.visited) == 2
    assert [row["status"] for row in candidates] == [
        "verified_siren_match",
        "verified_siren_match",
    ]
    con.close()


def test_new_company_match_cannot_inherit_another_companys_domain_proof(tmp_path):
    db = tmp_path / "france.sqlite3"
    con = add_company_and_candidate(db, siren="123456789", domain="brand-site.fr")
    upsert_candidate(
        con,
        country="FR",
        registry_id="987654321",
        legal_name="AUTRE GROUPE SAS",
        legal_form="5710",
        registry_status="active",
        registry_source="INSEE SIRENE",
        legal_person=True,
        raw={"employer_scope": "employer", "employee_range": "11"},
    )
    http = FakeHttp({
        "https://brand-site.fr/": '<a href="/mentions-legales">Mentions légales</a>',
        "https://brand-site.fr/mentions-legales": "<p>SIREN : 123 456 789</p>",
    })

    first = verify_pending_candidates(con, http, limit=10)
    assert first["verified_by_siren"] == 1
    proof = con.execute(
        "SELECT verified_registry_id,status FROM france_website_candidates"
    ).fetchone()
    assert proof["verified_registry_id"] == "123456789"
    assert proof["status"] == "verified_siren_match"

    _upsert_candidate(
        con,
        place_id="overture-place-second-identity",
        place_name="EXEMPLE INDUSTRIE",
        website_url="https://brand-site.fr/",
        domain="brand-site.fr",
        normalized_name="exemple industrie",
        registry_ids=["987654321"],
        match_method="exact_trading_name",
        address=None,
        locality=None,
        postcode=None,
        region=None,
        confidence=0.95,
        release="2026-11-18.0",
        observed_names=[{"value": "EXEMPLE INDUSTRIE", "language": "fr"}],
    )
    reset = con.execute(
        "SELECT status,verified_registry_id,verification_note FROM france_website_candidates"
    ).fetchone()
    assert reset["status"] == "pending_verification"
    assert reset["verified_registry_id"] is None
    assert reset["verification_note"] is None

    verify_pending_candidates(con, http, limit=10)
    output = tmp_path / "france-company-websites.json"
    count = export_verified_websites(con, output)
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert count == 1
    assert [row["siren"] for row in payload["companies"]] == ["123456789"]
    assert payload["companies"][0]["verified_registry_id"] == "123456789"
    con.close()



def test_french_tech_cohort_creates_unverified_candidates_then_verifies_company_identity(tmp_path):
    from crawler.france_website_discovery import FRENCH_TECH_NEXT40_120_URL

    db = tmp_path / "france.sqlite3"
    con = connect(db)
    upsert_candidate(
        con,
        country="FR",
        registry_id="123456789",
        legal_name="EXEMPLE STARTUP SAS",
        legal_form="5710",
        registry_status="active",
        registry_source="INSEE SIRENE",
        legal_person=True,
        raw={"employer_scope": "employer", "employee_range": "11"},
    )
    cohort_page = (
        '<html><a href="https://startup.example/">Exemple Startup</a>'
        '<a href="https://www.linkedin.com/company/exemple-startup">LinkedIn</a></html>'
    )
    http = FakeHttp({
        FRENCH_TECH_NEXT40_120_URL: cohort_page,
        "https://startup.example/": '<a href="/mentions-legales">Mentions légales</a>',
        "https://startup.example/mentions-legales": "<p>SIREN : 123 456 789</p>",
    })

    report = ingest_french_tech_next40_120(con, http)
    candidate = con.execute(
        "SELECT status,match_method,source_evidence_json FROM france_website_candidates"
    ).fetchone()
    assert report["companies_discovered"] == 1
    assert candidate["status"] == "pending_verification"
    assert candidate["match_method"] == "french_tech_official_cohort"
    evidence = json.loads(candidate["source_evidence_json"])
    assert evidence[0]["provider"] == "Mission French Tech"
    assert evidence[0]["source_url"] == FRENCH_TECH_NEXT40_120_URL
    assert "licence_status" in evidence[0]

    verification = verify_pending_candidates(con, http, limit=10)
    company = con.execute(
        "SELECT official_domain,domain_verified,raw_json FROM companies WHERE registry_id='123456789'"
    ).fetchone()
    raw = json.loads(company["raw_json"])
    assert verification["verified_by_siren"] == 1
    assert company["official_domain"] == "startup.example"
    assert company["domain_verified"] == 1
    assert raw["startup_cohort_verified"] is True
    assert raw["startup_discovery_sources"][0]["legal_notice_url"] == "https://startup.example/mentions-legales"
    con.close()
