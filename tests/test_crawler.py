"""Offline checks for the source-first Europe job pipeline."""
import json
from datetime import datetime, timezone
from pathlib import Path

from crawler import adapters, discover, geo
from crawler.company_security import is_company_approved, screen_vacancy, validate_vacancy_source
from crawler.crawl import crawl_company, load_companies, merge
from crawler.http import FetchError
from crawler.normalize import detect_language, find_salary, normalize

NOW = "2026-11-23"


class FakeHttp:
    def __init__(self, routes):
        self.routes = routes

    def _get(self, url):
        if url not in self.routes:
            raise FetchError(f"404 not found: {url}")
        return self.routes[url]

    def get_json(self, url, **kwargs):
        return self._get(url)

    def get_text(self, url, **kwargs):
        return self._get(url)

    def allowed_by_robots(self, url):
        return True


def co(ats="greenhouse", slug="acme", hq="DE", **kw):
    board = {
        "greenhouse": f"https://boards.greenhouse.io/{slug}",
        "lever": f"https://jobs.lever.co/{slug}",
        "ashby": f"https://jobs.ashbyhq.com/{slug}",
        "workable": f"https://apply.workable.com/{slug}",
    }[ats]
    careers = "https://acme.example/careers"
    checked = datetime.now(timezone.utc).date().isoformat()
    verification = {
        "mode": "curated_public_ats", "status": "approved",
        "domain": {"url": "https://acme.example", "verified": True, "method": "public_ats_board_review"},
        "source": {"approved": True, "verified": True, "method": "public_ats_board_review",
                   "career_url": board, "evidence_url": careers, "checked_at": checked},
    }
    return {"name": "Acme", "slug": slug, "ats_slug": slug, "ats": ats, "hq": hq,
            "official_domain": "acme.example", "official_careers_url": careers,
            "career_url": board, "verification": verification, **kw}


def test_europe_geography_and_map_region():
    assert geo.locate("Berlin, Germany")["country"] == "DE"
    assert geo.locate("München")["city"] == "Munich"
    assert geo.locate("Warsaw, Poland")["country"] == "PL"
    assert geo.locate("Cambridge, MA", "US") is None
    assert geo.locate("Remote Europe")["country"] == "EU"


def test_europe_only_and_remote_rules():
    base = {"id": "1", "title": "Backend Engineer", "url": "https://boards.greenhouse.io/acme/jobs/1"}
    assert normalize({**base, "location": "Paris, France"}, co(), NOW)["country"] == "FR"
    assert normalize({**base, "location": "Berlin, Germany"}, co(), NOW)["country"] == "DE"
    assert normalize({**base, "location": "London, UK"}, co(), NOW)["country"] == "GB"
    assert normalize({**base, "location": "New York, NY"}, co(), NOW) is None
    assert normalize({**base, "location": "Remote - US", "remote": True}, co(), NOW) is None
    assert normalize({**base, "location": "Remote Europe", "remote": True}, co(hq="US"), NOW)["country"] == "EU"
    assert normalize({**base, "location": "Remote", "remote": True}, co(hq="GB"), NOW)["country"] == "EU"
    assert normalize({**base, "location": "Remote", "remote": True}, co(hq="US"), NOW) is None


def test_remote_worldwide_and_non_european_country_hints_are_rejected():
    base = {"id": "1", "title": "Designer", "url": "https://boards.greenhouse.io/acme/jobs/1"}
    assert normalize({**base, "location": "Remote worldwide", "remote": True}, co(), NOW) is None
    assert normalize({**base, "location": "Berlin", "country": "US"}, co(), NOW) is None


def test_salary_language_and_role_normalisation():
    assert find_salary("Salary: €60,000 - €80,000 per year") == {"min": 60000, "max": 80000, "currency": "EUR"}
    assert find_salary("£45.000 – £60.000")["currency"] == "GBP"
    assert detect_language("short") == "en"
    job = normalize({"id": "1", "title": "Senior Data Scientist",
                     "url": "https://boards.greenhouse.io/acme/jobs/1", "location": "Berlin"},
                    co(), NOW)
    assert (job["role"], job["level"]) == ("data", "senior")
    other = normalize({"id": "2", "title": "Mystery Professional Role",
                       "url": "https://boards.greenhouse.io/acme/jobs/2", "location": "Berlin"},
                      co(), NOW)
    assert other["role"] == "other"


def test_public_ats_adapters_and_discovery():
    http = FakeHttp({
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true": {"jobs": [{
            "id": 7, "title": "Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/7",
            "location": {"name": "Amsterdam, Netherlands"}, "departments": [{"name": "Engineering"}],
            "updated_at": "2026-11-01T10:00:00-05:00", "content": "&lt;p&gt;Build &lt;b&gt;things&lt;/b&gt;&lt;/p&gt;"}]},
        "https://api.lever.co/v0/postings/acme?mode=json": [{
            "id": "abc", "text": "Designer", "hostedUrl": "https://jobs.lever.co/acme/abc",
            "categories": {"location": "Stockholm", "team": "Design"}, "createdAt": 1763900000000,
            "descriptionPlain": "Design stuff"}],
        "https://api.ashbyhq.com/posting-api/job-board/acme?includeCompensation=true": {"jobs": [{
            "id": "a1", "title": "PM", "applyUrl": "https://jobs.ashbyhq.com/acme/a1/application",
            "location": "London", "isRemote": False, "publishedAt": "2026-11-10T09:00:00Z",
            "descriptionPlain": "Own the roadmap", "isListed": True}]},
        "https://apply.workable.com/api/v1/widget/accounts/acme?details=true": {"jobs": [{
            "shortcode": "W1", "title": "Ops", "application_url": "https://apply.workable.com/acme/j/W1/apply",
            "city": "Lisbon", "country": "Portugal", "telecommuting": True, "published_on": "2026-11-05"}]},
    })
    assert adapters.greenhouse("acme", http)[0]["description"] == "Build things"
    assert adapters.lever("acme", http)[0]["location"] == "Stockholm"
    assert adapters.ashby("acme", http)[0]["url"].endswith("/application")
    assert adapters.workable("acme", http)[0]["remote"] is True
    hits = discover.detect('<a href="https://jobs.lever.co/foo">Lever</a><a href="https://jobs.ashbyhq.com/bar">Ashby</a>')
    assert ("lever", "foo") in hits and ("ashby", "bar") in hits


def test_reviewed_source_and_vacancy_url_gates():
    company = co()
    assert is_company_approved(company) == (True, None)
    assert validate_vacancy_source("https://boards.greenhouse.io/acme/jobs/12", company)[0] is True
    assert validate_vacancy_source("https://boards.greenhouse.io/acme-imposter/jobs/12", company)[0] is False
    company["verification"]["source"]["approved"] = False
    assert not is_company_approved(company)[0]


def test_scam_and_manual_trade_checks():
    company = co()
    raw = {"title": "Plumber", "url": "https://boards.greenhouse.io/acme/jobs/1", "description": ""}
    assert screen_vacancy(raw, company)[0] == "reject"
    raw = {"title": "Finance Analyst", "url": "https://boards.greenhouse.io/acme/jobs/2",
           "description": "Pay an application fee to process the recruitment."}
    assert screen_vacancy(raw, company)[0] == "reject"
    raw = {"title": "Finance Analyst", "url": "https://boards.greenhouse.io/acme/jobs/3",
           "description": "Contact our recruiter on WhatsApp."}
    assert screen_vacancy(raw, company)[0] == "review"


def test_crawler_uses_companies_yaml_only():
    companies = load_companies(Path("companies.yaml"))
    assert len(companies) == 7
    assert {company["ats"] for company in companies} == {"lever", "ashby", "workable", "greenhouse"}
    assert all(is_company_approved(company)[0] for company in companies)


def test_feed_failures_are_isolated_and_jobs_expire():
    missing = crawl_company(co(slug="missing"), FakeHttp({}), NOW)
    assert not missing["ok"] and "404" in missing["error"]
    now = datetime(2026, 11, 23, tzinfo=timezone.utc)
    old_fresh = {"id": "b:1", "company_slug": "b", "last_seen": "2026-11-22"}
    old_stale = {"id": "c:1", "company_slug": "c", "last_seen": "2026-11-01"}
    retained = merge([old_fresh, old_stale], [], now)
    assert [job["id"] for job in retained] == ["b:1"]


def test_public_job_file_contains_no_sample_jobs():
    payload = json.loads(Path("site/data/jobs.json").read_text(encoding="utf-8"))
    assert payload.get("demo") is False
    assert payload.get("count") == len(payload.get("jobs", []))
    assert all("Northwind Labs" not in str(job.get("company", "")) for job in payload["jobs"])
