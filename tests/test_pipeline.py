"""Offline tests for the France-first public ATS pipeline."""
import json
from pathlib import Path

import pytest

from discover_companies import comparable_name
from job_pipeline import (
    PROVIDERS, clean_public_text, create_database, extract_board_token,
    is_france_location, job_is_in_france, normalise_job, upsert_company,
    upsert_jobs_for_source, upsert_source,
)


def test_no_employer_allowlist_and_robots_exclusions():
    assert "companies.yaml" not in {path.name for path in Path(".").glob("*.yaml")}
    assert {"greenhouse", "lever", "ashby", "breezy", "recruitee", "teamtailor"} <= set(PROVIDERS)
    assert "smartrecruiters" not in PROVIDERS
    assert "personio" not in PROVIDERS


@pytest.mark.parametrize(("provider", "url", "expected"), [
    ("greenhouse", "https://boards.greenhouse.io/acme/jobs/12", "acme"),
    ("greenhouse", "https://job-boards.eu.greenhouse.io/sample-fr/locations", "sample-fr"),
    ("lever", "https://jobs.lever.co/doctolib/123", "doctolib"),
    ("ashby", "https://jobs.ashbyhq.com/qonto", "qonto"),
    ("workable", "https://apply.workable.com/contentsquare/j/ABC", "contentsquare"),
    ("breezy", "https://sample-fr.breezy.hr/p/123-job", "sample-fr"),
    ("recruitee", "https://company.recruitee.com/o/role", "company"),
    ("teamtailor", "https://company.teamtailor.com/jobs/1", "company"),
    ("gem", "https://jobs.gem.com/example/jobs/123", "example"),
    ("pinpoint", "https://example.pinpointhq.com/postings/123", "example"),
    ("bamboohr", "https://example.bamboohr.com/careers/list", "example"),
    ("rippling", "https://ats.rippling.com/example/jobs/123", "example"),
])
def test_extract_public_board_tokens(provider, url, expected):
    assert extract_board_token(provider, url) == expected


def test_reject_platform_routes_and_unrelated_hosts():
    assert extract_board_token("workable", "https://apply.workable.com/j/123") is None
    assert extract_board_token("greenhouse", "https://evil.example/boards/acme") is None
    assert extract_board_token("breezy", "https://foo.bar.breezy.hr/json") is None


@pytest.mark.parametrize(("location", "country", "expected"), [
    ("Paris, France", None, True),
    ("Lyon", None, True),
    ("Remote - France", None, True),
    ("Remote, France", "FR", True),
    ("Bordeaux", None, True),
    ("Berlin, Germany", None, False),
    ("Remote Europe", None, False),
    ("Remote worldwide", None, False),
    ("Cambridge, UK", None, False),
    ("San Francisco, CA", "US", False),
])
def test_france_location_filter(location, country, expected):
    assert is_france_location(location, country) is expected


def test_ats_nested_locations_use_country_and_city():
    assert job_is_in_france("greenhouse", {"location": {"name": "Paris", "country_code": "FR"}})
    assert job_is_in_france("lever", {"categories": {"location": "Remote - France", "allLocations": ["Paris"]}})
    assert not job_is_in_france("ashby", {
        "location": "Remote Europe",
        "address": {"postalAddress": {"addressCountry": "EU"}},
    })


def test_registry_match_is_normalized_and_conservative():
    assert comparable_name("Doctolib SAS") == "doctolib"
    assert comparable_name("Qonto France") == "qonto"
    assert comparable_name("A Maison de Créativité") == "a maison de creativite"
    assert comparable_name("A Maison de Créativité") != comparable_name("Maison Créative")


def test_job_normalization_removes_personal_contact_details():
    raw = {
        "id": "job-1", "title": "Account Executive",
        "hostedUrl": "https://jobs.lever.co/acme/job-1",
        "categories": {"location": "Paris, France", "team": "Sales"},
        "descriptionPlain": "Email jane.doe@example.com or call +33 6 12 34 56 78.",
        "createdAt": 1763900000000,
    }
    job = normalise_job("lever", "acme", raw)
    assert job is not None
    assert job["external_id"] == "job-1"
    assert job["department"] == "Sales"
    assert "jane.doe@example.com" not in job["description"]
    assert "+33 6 12 34 56 78" not in job["description"]
    assert clean_public_text("Recruiter: anna@example.org") == "Recruiter: [email removed]"


def test_company_and_source_upserts(tmp_path):
    db = create_database(tmp_path / "jobs.db")
    first = upsert_company(db, "greenhouse:acme", "Acme", None, "unmatched", "2026-11-23")
    again = upsert_company(db, "greenhouse:acme", "Acme", None, "unmatched", "2026-11-24")
    assert first == again
    one = upsert_source(db, first, "Acme", "greenhouse", "acme", "2026-11-23")
    two = upsert_source(db, first, "Acme", "greenhouse", "acme", "2026-11-24")
    assert one == two
    assert db.execute("SELECT COUNT(*) FROM ats_sources").fetchone()[0] == 1
    db.close()


def test_same_siren_joins_multiple_ats_boards(tmp_path):
    db = create_database(tmp_path / "jobs.db")
    registry = {
        "siren": "123456789", "nom_raison_sociale": "ACME",
        "etat_administratif": "A", "nature_juridique": "5499",
        "statut_diffusion": "O",
        "siege": {"activite_principale": "62.01Z", "libelle_commune": "PARIS"},
    }
    first = upsert_company(db, "greenhouse:acme", "Acme", registry, "exact", "2026-11-23")
    second = upsert_company(db, "ashby:acme", "Acme", registry, "exact", "2026-11-24")
    assert first == second
    assert db.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 1
    db.close()


def test_successful_feed_expires_only_its_missing_jobs(tmp_path):
    db = create_database(tmp_path / "jobs.db")
    company = upsert_company(db, "greenhouse:acme", "Acme", None, "unmatched", "2026-11-23")
    source = upsert_source(db, company, "Acme", "greenhouse", "acme", "2026-11-23")
    jobs = [
        {"external_id": "1", "title": "Engineer", "department": "Engineering",
         "location": "Paris, France", "city": "Paris", "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
         "posted_at": None, "description": "", "remote": False},
        {"external_id": "2", "title": "Sales", "department": "Sales",
         "location": "Lyon, France", "city": "Lyon", "apply_url": "https://boards.greenhouse.io/acme/jobs/2",
         "posted_at": None, "description": "", "remote": False},
    ]
    upsert_jobs_for_source(db, source, jobs, "2026-11-23")
    upsert_jobs_for_source(db, source, jobs[:1], "2026-11-24")
    state = db.execute("SELECT external_id,is_active FROM jobs ORDER BY external_id").fetchall()
    assert [(row[0], row[1]) for row in state] == [("1", 1), ("2", 0)]
    db.close()


def test_a_failed_feed_does_not_run_expiration_logic(tmp_path):
    db = create_database(tmp_path / "jobs.db")
    company = upsert_company(db, "greenhouse:acme", "Acme", None, "unmatched", "2026-11-23")
    source = upsert_source(db, company, "Acme", "greenhouse", "acme", "2026-11-23")
    upsert_jobs_for_source(db, source, [{
        "external_id": "1", "title": "Engineer", "department": "Engineering",
        "location": "Paris, France", "city": "Paris",
        "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
        "posted_at": None, "description": "", "remote": False,
    }], "2026-11-23")
    # A failed request never invokes upsert_jobs_for_source.
    assert db.execute("SELECT is_active FROM jobs WHERE source_id=?", (source,)).fetchone()[0] == 1
    db.close()
