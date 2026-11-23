"""Offline checks for the source-first Europe job pipeline."""
import json
from datetime import datetime, timezone
from pathlib import Path

from crawler import adapters, discover, geo
from crawler.company_security import is_company_approved, screen_vacancy, validate_vacancy_source
from crawler.crawl import crawl_company, load_companies, merge
from crawler.source_discovery import extract_board
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
    return {
        "name": "Acme", "slug": slug, "ats_slug": slug, "ats": ats, "hq": hq,
        "company_key": f"{ats}:{slug}", "board_url": board,
        "verification": {
            "mode": "discovered_public_ats", "status": "approved",
            "source": {"board_url": board, "discovery_source": "Common Crawl"},
        },
        **kw,
    }


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
    company["verification"]["status"] = "blocked"
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


def test_crawler_uses_generated_board_index_not_company_allowlist(tmp_path):
    board_path = tmp_path / "boards.json"
    board_path.write_text(json.dumps({
        "source": "Common Crawl",
        "boards": [co(), co("ashby", "another-employer")],
    }), encoding="utf-8")
    companies = load_companies(board_path)
    assert len(companies) == 2
    assert {company["ats"] for company in companies} == {"greenhouse", "ashby"}
    assert all(is_company_approved(company)[0] for company in companies)
    assert not Path("companies.yaml").exists()


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


def _public_board(ats="greenhouse", slug="sample-employer", hq="DE"):
    urls = {
        "greenhouse": f"https://boards.greenhouse.io/{slug}",
        "lever": f"https://jobs.lever.co/{slug}",
        "ashby": f"https://jobs.ashbyhq.com/{slug}",
        "workable": f"https://apply.workable.com/{slug}",
        "smartrecruiters": f"https://jobs.smartrecruiters.com/{slug}",
        "personio": f"https://{slug}.jobs.personio.de/xml?language=en",
        "recruitee": f"https://{slug}.recruitee.com/api/offers/",
        "teamtailor": f"https://{slug}.teamtailor.com/jobs.rss",
        "breezy": f"https://{slug}.breezy.hr/",
        "pinpoint": f"https://{slug}.pinpointhq.com/postings.json",
        "bamboohr": f"https://{slug}.bamboohr.com/careers/list",
        "jazzhr": f"https://app.jazz.co/feeds/export/jobs/{slug}",
        "keka": f"https://{slug}.keka.com/careers",
        "homerun": f"https://feed.homerun.co/{slug}",
    }
    return {
        "name": "Sample Employer", "slug": slug, "ats_slug": slug,
        "company_key": ats + ":" + slug, "ats": ats, "hq": hq,
        "board_url": urls[ats],
        "verification": {"mode": "discovered_public_ats", "status": "approved"},
    }


def test_board_discovery_extracts_providers_without_company_whitelist():
    examples = [
        ("greenhouse", "https://boards.greenhouse.io/sample-employer"),
        ("lever", "https://jobs.lever.co/sample-employer"),
        ("ashby", "https://jobs.ashbyhq.com/sample-employer"),
        ("workable", "https://apply.workable.com/sample-employer"),
        ("smartrecruiters", "https://jobs.smartrecruiters.com/sample-employer"),
        ("personio", "https://sample-employer.jobs.personio.de"),
        ("recruitee", "https://sample-employer.recruitee.com"),
        ("teamtailor", "https://sample-employer.teamtailor.com/jobs"),
        ("workday", "https://sample-employer.wd5.myworkdayjobs.com/en-US/Careers"),
        ("breezy", "https://sample-employer.breezy.hr"),
        ("pinpoint", "https://sample-employer.pinpointhq.com/postings.json"),
        ("bamboohr", "https://sample-employer.bamboohr.com/careers/list"),
        ("jazzhr", "https://sample-employer.applytojob.com/apply"),
        ("keka", "https://sample-employer.keka.com/careers"),
        ("homerun", "https://feed.homerun.co/sample-employer"),
    ]
    for provider, url in examples:
        found = extract_board(provider, url)
        assert found and found["ats"] == provider
        assert found["slug"]


def test_company_gate_validates_provider_and_board_not_company_name():
    company = _public_board()
    assert is_company_approved(company) == (True, None)
    wrong = {**company, "board_url": "https://boards.greenhouse.io/someone-else"}
    assert is_company_approved(wrong)[0] is False


def test_untrusted_wrong_board_url_is_rejected_but_feed_returned_apply_url_is_allowed():
    from crawler.company_security import validate_vacancy_source
    company = _public_board()
    assert not validate_vacancy_source("https://boards.greenhouse.io/another-company/jobs/1", company)[0]
    assert validate_vacancy_source(
        "https://careers.sample-employer.example/jobs/1", company, trusted_feed=True
    )[0]


def test_loader_uses_board_index_json_not_companies_yaml(tmp_path):
    board_path = tmp_path / "boards.json"
    board_path.write_text(json.dumps({"source": "Common Crawl", "boards": [
        _public_board(), _public_board("ashby", "another-employer")
    ]}), encoding="utf-8")
    items = load_companies(board_path)
    assert len(items) == 2
    assert {item["ats"] for item in items} == {"greenhouse", "ashby"}


def test_new_public_board_adapters():
    routes = {
        "https://sample.breezy.hr/json": [{"id": "b1", "name": "Sales Manager", "location": "Paris"}],
        "https://sample.pinpointhq.com/postings.json": {"postings": [
            {"id": "p1", "title": "Account Executive", "url": "/postings/p1", "location": "Brussels"}]},
        "https://sample.bamboohr.com/careers/list": {"result": [
            {"id": "ba1", "jobOpeningName": "Marketing Manager", "location": "Berlin"}]},
        "https://app.jazz.co/feeds/export/jobs/sample": (
            "<jobs><job><id>j1</id><title>Analyst</title><city>Amsterdam</city></job></jobs>"),
        "https://sample.keka.com/careers": (
            '<a href="/ats/documents/123e4567-e89b-12d3-a456-426614174000/careerportal">Careers</a>'),
        "https://sample.keka.com/careers/api/embedjobs/default/active/123e4567-e89b-12d3-a456-426614174000": [
            {"jobId": "k1", "title": "Consultant", "location": "Paris"}],
        "https://feed.homerun.co/sample": (
            '<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>h1</id><title>Designer</title>'
            '<link href="https://feed.homerun.co/sample/jobs/h1"/><summary>Design</summary></entry></feed>'),
    }
    http = FakeHttp(routes)
    assert adapters.breezy("sample", http)[0]["id"] == "b1"
    assert adapters.pinpoint("sample", http)[0]["url"] == "https://sample.pinpointhq.com/postings/p1"
    assert adapters.bamboohr("sample", http)[0]["title"] == "Marketing Manager"
    assert adapters.jazzhr("sample", http)[0]["title"] == "Analyst"
    assert adapters.keka("sample", http)[0]["id"] == "k1"
    assert adapters.homerun("sample", http)[0]["title"] == "Designer"


def test_europe_remote_is_accepted_when_location_is_explicit():
    base = {"id": "1", "title": "Product Manager", "url": "https://jobs.ashbyhq.com/sample/1"}
    assert normalize({**base, "location": "Remote Europe", "remote": True},
                     _public_board("ashby", hq="US"), NOW)["country"] == "EU"
