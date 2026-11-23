"""Tests for the Europe-wide ATS job discovery pipeline."""
from __future__ import annotations

from ats_pipeline import (
    choose_insee_result, export_data, fetch_ats_board, init_db, is_france_location,
    normalise_wttj_hit, parse_wttj_env, store_board_result, upsert_company, upsert_job,
    is_europe_location, is_recent_posting, is_professional_job,
)
from commoncrawl_index import board_record, surt_prefix, token_from_url


def test_france_location_requires_france_evidence():
    assert is_france_location("Paris, France") == (True, "Paris")
    assert is_france_location("Lyon", "FR") == (True, "Lyon")
    assert is_france_location("Nantes, FRANCE")[0] is True
    assert is_france_location("Remote France", None)[0] is True
    assert is_france_location("Remote Europe", None)[0] is False
    assert is_france_location("Remote", None)[0] is False
    assert is_france_location("New York, USA")[0] is False
    assert is_france_location("Paris, France", "US")[0] is False

def test_europe_location_and_29_day_freshness():
    from datetime import date
    assert is_europe_location("Berlin", "DE") == (True, "DE")
    assert is_europe_location("Amsterdam, Netherlands") == (True, "NL")
    assert is_europe_location("London, UK") == (True, "GB")
    assert is_europe_location("Remote Europe") == (True, "EU")
    assert is_europe_location("New York, USA", "US") == (False, None)
    assert is_recent_posting("2026-11-23", today=date(2026, 11, 23))
    assert is_recent_posting("2026-10-25", today=date(2026, 11, 23))
    assert not is_recent_posting("2026-10-24", today=date(2026, 11, 23))
    assert not is_recent_posting(None, today=date(2026, 11, 23))
    assert is_professional_job("Account Executive")
    assert not is_professional_job("Plumber")


def test_commoncrawl_board_tokens_are_provider_agnostic():
    assert surt_prefix("boards.greenhouse.io", "path") == "io,greenhouse,boards)/"
    assert surt_prefix("teamtailor.com", "subdomain") == "com,teamtailor,"
    assert token_from_url("greenhouse", "boards.greenhouse.io", "path",
        "https://boards.greenhouse.io/example-company/jobs/123") == "example-company"
    assert token_from_url("teamtailor", "teamtailor.com", "subdomain",
        "https://acme.teamtailor.com/jobs.rss") == "acme"
    assert token_from_url("teamtailor", "teamtailor.com", "subdomain",
        "https://acme.evil.com/jobs.rss") is None
    assert token_from_url("greenhouse", "boards.greenhouse.io", "path",
        "https://boards.greenhouse.io/robots.txt") is None


def test_board_records_preserve_eu_endpoint():
    board = board_record("lever", "jobs.eu.lever.co", "exampleco", "test")
    assert board["ats_slug"] == "exampleco"
    assert board["eu_endpoint"] is True
    assert board["board_url"] == "https://jobs.eu.lever.co/exampleco"


def test_smartrecruiters_board_discovery():
    board = board_record("smartrecruiters", "careers.smartrecruiters.com", "exampleco", "test")
    assert board["board_url"] == "https://careers.smartrecruiters.com/exampleco"
    assert token_from_url(
        "smartrecruiters", "jobs.smartrecruiters.com", "path",
        "https://jobs.smartrecruiters.com/exampleco/123",
    ) == "exampleco"


def test_wttj_public_env_parser():
    app_id, api_key = parse_wttj_env(
        'window.env = {"PUBLIC_ALGOLIA_APPLICATION_ID":"AB12CD34",'
        '"PUBLIC_ALGOLIA_API_KEY_CLIENT":"1234567890abcdef123456"};'
    )
    assert app_id == "AB12CD34"
    assert api_key.startswith("1234")


def test_wttj_hit_keeps_each_employer_separate():
    hit = {
        "id": "posting-1", "name": "Account Executive", "slug": "account-executive",
        "organization": {
            "slug": "french-startup", "name": "French Startup",
            "website": "https://french-startup.example",
        },
        "offices": [{"city": "Paris", "country": "France", "country_code": "FR"}],
        "published_at_timestamp": 1795305600,
        "remote": "fulltime", "contract_type": "full_time",
    }
    raw = normalise_wttj_hit(hit)
    assert raw is not None
    assert raw["company_key"] == "wttj:french-startup"
    assert raw["company_ats_slug"] == "french-startup"
    assert raw["source_id"] == "french-startup:posting-1"
    assert raw["remote"] is True
    assert raw["country_code"] == "FR"
    assert raw["apply_url"].startswith("https://www.welcometothejungle.com/fr/companies/")


def test_insee_matching_excludes_non_active_individual_or_non_diffusible():
    company = "Exemple Technologie"
    good = {
        "siren": "123456789", "nom_complet": "EXEMPLE TECHNOLOGIE",
        "nature_juridique": "5720", "etat_administratif": "A", "statut_diffusion": "O",
    }
    individual = {**good, "siren": "123456780", "nature_juridique": "1000"}
    inactive = {**good, "siren": "123456781", "etat_administratif": "C"}
    private = {**good, "siren": "123456782", "statut_diffusion": "N"}
    match, score = choose_insee_result(company, [individual, inactive, private, good])
    assert match == good
    assert score >= 0.9


def test_insee_ambiguous_matches_are_not_guessed():
    a = {
        "siren": "123456789", "nom_complet": "EXEMPLE TECHNOLOGIE",
        "nature_juridique": "5720", "etat_administratif": "A", "statut_diffusion": "O",
    }
    b = {**a, "siren": "987654321", "nom_complet": "EXEMPLE TECHNOLOGIES"}
    match, _ = choose_insee_result("Exemple Technologie", [a, b])
    assert match is None


def _job(source_id, title, url, location="Paris, France"):
    return {
        "source_type": "greenhouse", "source_id": source_id,
        "title": title, "apply_url": url, "location": location,
        "country_code": "FR", "posted_at": "2026-11-20",
        "description": "An actual professional vacancy.",
    }


def test_successful_board_refresh_upserts_and_expires_missing_jobs(tmp_path):
    conn = init_db(tmp_path / "jobs.db")
    board = {
        "ats_type": "greenhouse", "ats_slug": "exampleco",
        "board_url": "https://boards.greenhouse.io/exampleco",
    }
    conn.execute("""
      INSERT INTO ats_boards(board_key,ats_type,ats_slug,board_url,discovery_source,first_seen_at,last_seen_at)
      VALUES('greenhouse:exampleco','greenhouse','exampleco',?,'test','2026-11-01','2026-11-01')
    """, (board["board_url"],))
    conn.commit()

    store_board_result(conn, board, [
        _job("job-1", "Sales Manager", "https://boards.greenhouse.io/exampleco/jobs/1"),
        _job("job-2", "Data Analyst", "https://boards.greenhouse.io/exampleco/jobs/2"),
        {**_job("job-us", "US Sales", "https://boards.greenhouse.io/exampleco/jobs/3", "New York, USA"),
         "country_code": "US"},
    ], "2026-11-23T06:00:00+00:00")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE is_active=1").fetchone()[0] == 2

    store_board_result(conn, board, [
        _job("job-2", "Data Analyst", "https://boards.greenhouse.io/exampleco/jobs/2"),
    ], "2026-11-24T06:00:00+00:00")
    conn.commit()
    active = {row["source_id"]: row["is_active"] for row in
              conn.execute("SELECT source_id,is_active FROM jobs")}
    assert active["greenhouse:exampleco:job-1"] == 0
    assert active["greenhouse:exampleco:job-2"] == 1
    conn.close()


def test_wttj_jobs_are_grouped_by_employer_without_a_fake_board(tmp_path):
    conn = init_db(tmp_path / "jobs.db")
    jobs = []
    for slug, name, job_id in [
        ("org-one", "Organisation One", "1"),
        ("org-two", "Organisation Two", "2"),
    ]:
        jobs.append({
            "source_type": "wttj", "source_id": f"{slug}:{job_id}",
            "company_key": f"wttj:{slug}", "company_ats_type": "wttj",
            "company_ats_slug": slug, "company_name": name,
            "title": "Product Manager", "apply_url": f"https://jobs.example/{job_id}",
            "location": "Paris, France", "country_code": "FR", "posted_at": "2026-11-20",
        })
    kept = store_board_result(
        conn, {"ats_type": "wttj", "ats_slug": "france-index"},
        jobs, "2026-11-23T00:00:00+00:00", deactivate=False, update_board=False,
    )
    assert kept == 2
    names = {row["company_key"]: row["name"]
             for row in conn.execute("SELECT company_key,name FROM companies")}
    assert names == {
        "wttj:org-one": "Organisation One",
        "wttj:org-two": "Organisation Two",
    }
    assert conn.execute("SELECT COUNT(*) FROM ats_boards").fetchone()[0] == 0
    conn.close()


def test_exports_group_jobs_by_naf_code(tmp_path):
    conn = init_db(tmp_path / "jobs.db")
    company = {
        "company_key": "greenhouse:exampleco", "name": "Example Co",
        "ats_type": "greenhouse", "ats_slug": "exampleco",
    }
    upsert_company(conn, company)
    conn.execute("""
      UPDATE companies SET siren='123456789',naf_code='62.01Z',
        naf_code_rev2='62.01Z',insee_match_status='matched'
      WHERE company_key='greenhouse:exampleco'
    """)
    upsert_job(conn, company, {
        "source_type": "greenhouse", "source_id": "greenhouse:exampleco:1",
        "title": "Data Analyst", "apply_url": "https://jobs.example/1",
        "location": "Paris, France", "city": "Paris",
        "posted_at": "2026-11-20", "description": "Description",
    })
    conn.commit()
    data = export_data(conn)
    assert data["count"] == 1
    assert data["naf_groups"]["62.01Z"][0]["siren"] == "123456789"
    conn.close()



def test_smartrecruiters_public_feed_parses_france_posts():
    import asyncio

    class FakeSmartRecruitersHTTP:
        async def robots_allowed(self, url):
            return True

        async def get_json(self, url, **kwargs):
            return {
                "totalFound": 1,
                "content": [{
                    "id": "posting-42",
                    "name": "Account Executive",
                    "releasedDate": "2026-11-22",
                    "location": {
                        "city": "Paris",
                        "country": "France",
                        "fullLocation": "Paris, France",
                    },
                    "department": {"label": "Sales"},
                }],
            }

    board = {
        "ats_type": "smartrecruiters",
        "ats_slug": "exampleco",
        "board_url": "https://careers.smartrecruiters.com/exampleco",
    }
    rows, error = asyncio.run(fetch_ats_board(FakeSmartRecruitersHTTP(), board))
    assert error is None
    assert len(rows) == 1
    assert rows[0]["title"] == "Account Executive"
    assert rows[0]["country_code"] == "FR"
    assert rows[0]["apply_url"] == "https://jobs.smartrecruiters.com/exampleco/posting-42"
