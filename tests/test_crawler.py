"""Offline tests: run with  python -m pytest -q   (no network needed)."""
import json
from datetime import datetime, timezone

from crawler import adapters, discover, geo
from crawler.crawl import crawl_company, merge
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

    def get_json(self, url, **kw):
        return self._get(url)

    def get_text(self, url, **kw):
        return self._get(url)

    def post_json(self, url, payload, **kw):
        return self._get(url)

    def allowed_by_robots(self, url):
        return True


def co(ats="greenhouse", slug="acme", hq="DE", **kw):
    verification = {
        "status": "approved",
        "registry": {"verified": True, "country": hq, "id": "123456", "status": "active", "legal_person": True,
                     "checked_at": datetime.now(timezone.utc).date().isoformat()},
        "domain": {"url": "https://acme.example", "verified": True, "method": "manual_documented_review",
                   "checked_at": datetime.now(timezone.utc).date().isoformat()},
        "source": {"approved": True, "verified": True, "method": "manual_documented_review",
                   "career_url": "https://acme.example/careers", "checked_at": datetime.now(timezone.utc).date().isoformat()},
    }
    return {"name": "Acme", "slug": slug, "ats_slug": slug, "ats": ats, "hq": hq,
            "official_domain": "acme.example", "verification": verification, **kw}


# ------------------------------------------------------------------ geo
def test_geo_city_and_country():
    assert geo.locate("Berlin, Germany")["city"] == "Berlin"
    assert geo.locate("München")["city"] == "Munich"
    assert geo.locate("Somewhere in Poland")["country"] == "PL"
    assert geo.locate("Cambridge, MA", "US") is None
    assert geo.locate("Frankfurt am Main")["country"] == "DE"


# ------------------------------------------------------------ normalisation
def test_salary_parsing():
    assert find_salary("Salary: €60,000 - €80,000 per year") == {"min": 60000, "max": 80000, "currency": "EUR"}
    assert find_salary("between 55k to 70k EUR")["min"] == 55000
    assert find_salary("£45.000 – £60.000")["currency"] == "GBP"
    assert find_salary("we pay well") is None


def test_language_detection():
    de = "Wir suchen für unser Team eine Person mit Erfahrung und Freude bei der Arbeit von zu Hause und mit uns"
    assert detect_language(de) == "de"
    assert detect_language("short") == "en"


def test_france_only_filter_and_remote_rules():
    base = {"id": "1", "title": "Backend Engineer", "url": "https://x.test/1", "description": ""}
    assert normalize({**base, "location": "Paris, France"}, co(hq="DE"), NOW)["country"] == "FR"
    assert normalize({**base, "location": "Berlin"}, co(hq="FR"), NOW) is None
    assert normalize({**base, "location": "New York, NY"}, co(hq="FR"), NOW) is None
    assert normalize({**base, "location": "Remote - US"}, co(hq="FR"), NOW) is None
    # A French employer may publish a generic remote role; a foreign employer may not.
    assert normalize({**base, "location": "Remote"}, co(hq="FR"), NOW)["country"] == "FR"
    assert normalize({**base, "location": "Remote"}, co(hq="US"), NOW) is None
    # EMEA is not a France location and must not bypass the France-only rule.
    assert normalize({**base, "location": "Remote, EMEA"}, co(hq="US"), NOW) is None
    assert normalize({**base, "location": "Remote - Germany"}, co(hq="US"), NOW) is None
    assert normalize({**base, "location": "Remote - France"}, co(hq="US"), NOW)["country"] == "FR"


def test_classification():
    j = normalize({"id": "1", "title": "Senior Data Scientist", "url": "https://x.test/1", "location": "Paris"}, co(), NOW)
    assert (j["role"], j["level"]) == ("data", "senior")
    j = normalize({"id": "2", "title": "Software Engineering Intern", "url": "https://x.test/2", "location": "Paris, France"}, co(), NOW)
    assert (j["role"], j["level"]) == ("engineering", "intern")


def test_snippet_is_truncated():
    j = normalize({"id": "1", "title": "DevOps Engineer", "url": "https://x.test/1", "location": "Paris, France",
                   "description": "word " * 500}, co(), NOW)
    assert len(j["snippet"]) <= 500


# ---------------------------------------------------------------- adapters
def test_greenhouse():
    http = FakeHttp({"https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true": {"jobs": [{
        "id": 7, "title": "Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/7",
        "location": {"name": "Amsterdam, Netherlands"}, "departments": [{"name": "Engineering"}],
        "updated_at": "2026-11-01T10:00:00-05:00", "content": "&lt;p&gt;Build &lt;b&gt;things&lt;/b&gt;&lt;/p&gt;"}]}})
    jobs = adapters.greenhouse("acme", http)
    assert jobs[0]["description"] == "Build things" and jobs[0]["posted"] == "2026-11-01"


def test_lever_ashby_recruitee_workable_smartrecruiters():
    http = FakeHttp({
        "https://api.lever.co/v0/postings/acme?mode=json": [{
            "id": "abc", "text": "Designer", "hostedUrl": "https://jobs.lever.co/acme/abc",
            "categories": {"location": "Stockholm", "team": "Design"}, "workplaceType": "hybrid",
            "createdAt": 1763900000000, "descriptionPlain": "Design stuff"}],
        "https://api.ashbyhq.com/posting-api/job-board/acme?includeCompensation=true": {"jobs": [{
            "id": "a1", "title": "PM", "applyUrl": "https://jobs.ashbyhq.com/acme/a1/application",
            "location": "London", "isRemote": False, "publishedAt": "2026-11-10T09:00:00Z",
            "descriptionPlain": "Own the roadmap", "isListed": True},
            {"id": "a2", "title": "Hidden", "applyUrl": "https://x.test", "isListed": False}]},
        "https://acme.recruitee.com/api/offers/": {"offers": [{
            "id": 5, "title": "Sales", "careers_url": "https://acme.recruitee.com/o/sales",
            "city": "Utrecht", "country": "Netherlands", "country_code": "NL", "status": "published",
            "created_at": "2026-11-02 08:00:00 UTC", "description": "<p>Sell</p>"}]},
        "https://apply.workable.com/api/v1/widget/accounts/acme?details=true": {"jobs": [{
            "shortcode": "W1", "title": "Ops", "application_url": "https://apply.workable.com/acme/j/W1/apply",
            "city": "Lisbon", "country": "Portugal", "telecommuting": True, "published_on": "2026-11-05"}]},
        "https://api.smartrecruiters.com/v1/companies/acme/postings?limit=100&offset=0": {
            "totalFound": 1, "content": [{"id": "9", "name": "Analyst", "releasedDate": "2026-11-03T00:00:00Z",
            "location": {"city": "Vienna", "country": "at", "fullLocation": "Vienna, Austria"},
            "department": {"label": "Finance"}}]},
    })
    assert adapters.lever("acme", http)[0]["location"] == "Stockholm"
    ash = adapters.ashby("acme", http)
    assert len(ash) == 1 and ash[0]["url"].endswith("/application")
    assert adapters.recruitee("acme", http)[0]["country"] == "NL"
    w = adapters.workable("acme", http)[0]
    assert w["remote"] is True and w["location"] == "Lisbon, Portugal"
    sr = adapters.smartrecruiters("acme", http)[0]
    assert sr["url"] == "https://jobs.smartrecruiters.com/acme/9" and sr["country"] == "AT"


def test_personio_xml_and_domain_fallback():
    xml = """<?xml version="1.0"?><workzag-jobs>
      <position><id>111</id><office>München</office><department>Tech</department>
        <name>Backend Developer (m/w/d)</name><createdAt>2026-11-12T10:00:00+00:00</createdAt>
        <jobDescriptions><jobDescription><name>Aufgaben</name><value>&lt;p&gt;Wir suchen dich&lt;/p&gt;</value></jobDescription></jobDescriptions>
      </position></workzag-jobs>"""
    # .de domain is missing -> must fall back to .com
    http = FakeHttp({"https://acme.jobs.personio.com/xml?language=en": xml})
    jobs = adapters.personio("acme", http)
    assert jobs[0]["url"] == "https://acme.jobs.personio.com/job/111"
    assert jobs[0]["location"] == "München" and "Wir suchen dich" in jobs[0]["description"]
    final = normalize(jobs[0], co(ats="personio"), NOW)
    assert final is None  # Munich is outside the France-only job board


def test_teamtailor_rss():
    rss = """<?xml version="1.0"?><rss xmlns:tt="https://teamtailor.com/locations"><channel>
      <item><title>Frontend Dev</title><link>https://acme.teamtailor.com/jobs/1-frontend</link>
      <guid>tt-1</guid><description>&lt;p&gt;Hej&lt;/p&gt;</description>
      <tt:locations><tt:location><tt:city>Gothenburg</tt:city><tt:country>Sweden</tt:country></tt:location></tt:locations>
      </item></channel></rss>"""
    jobs = adapters.teamtailor("acme", FakeHttp({"https://acme.teamtailor.com/jobs.rss": rss}))
    assert jobs[0]["location"] == "Gothenburg, Sweden"


def test_jsonld():
    page = '''<html><script type="application/ld+json">{"@type":"JobPosting","title":"Data Engineer",
      "url":"https://acme.test/jobs/1","datePosted":"2026-11-20","description":"<p>Pipelines</p>",
      "identifier":{"@type":"PropertyValue","value":"J-1"},
      "jobLocation":{"address":{"addressLocality":"Warsaw","addressCountry":"PL"}}}</script></html>'''
    jobs = adapters.jsonld("acme", FakeHttp({"https://acme.test/careers": page}), urls=["https://acme.test/careers"])
    assert jobs[0]["id"] == "J-1" and jobs[0]["location"] == "Warsaw, PL"


def test_discover_detects_ats():
    page = '<a href="https://jobs.lever.co/foo">x</a><a href="https://bar.jobs.personio.de/">y</a>'
    assert ("lever", "foo") in discover.detect(page)
    assert ("personio", "bar") in discover.detect(page)


def test_jsonld_supports_graph_type_arrays_and_relative_job_urls():
    payload = {
        "@context": "https://schema.org",
        "@graph": [{
            "@type": ["Thing", "JobPosting"],
            "title": "Product Analyst",
            "url": "/jobs/product-analyst",
            "datePosted": "2026-11-20",
            "identifier": {"@type": "PropertyValue", "name": "Reference", "value": "PA-22"},
            "jobLocation": [
                {"address": {"addressLocality": "Paris", "addressCountry": "FR"}},
                {"address": {"addressLocality": "Lyon", "addressCountry": "FR"}},
            ],
            "description": "<p>Analyse product data</p>",
        }],
    }
    page = '<script type="application/ld+json">' + json.dumps(payload) + '</script>'
    jobs = adapters.jsonld(
        "example",
        FakeHttp({"https://example.test/careers": page}),
        urls=["https://example.test/careers"],
    )
    assert len(jobs) == 1
    assert jobs[0]["id"] == "PA-22"
    assert jobs[0]["url"] == "https://example.test/jobs/product-analyst"
    assert jobs[0]["location"] == "Paris, FR | Lyon, FR"


# ---------------------------------------------------------- crawl + merge
def test_crawl_company_isolates_errors():
    res = crawl_company(co(slug="missing"), FakeHttp({}), NOW)
    assert not res["ok"] and "404" in res["error"]


def test_merge_closes_vanished_jobs_but_survives_outages():
    now = datetime(2026, 11, 23, tzinfo=timezone.utc)
    old_a = {"id": "g:a:1", "company_slug": "a", "first_seen": "2026-11-01", "last_seen": "2026-11-22", "title": "t"}
    old_a2 = {"id": "g:a:2", "company_slug": "a", "first_seen": "2026-11-01", "last_seen": "2026-11-22", "title": "t"}
    old_b = {"id": "g:b:1", "company_slug": "b", "first_seen": "2026-11-01", "last_seen": "2026-11-22", "title": "t"}
    stale_c = {"id": "g:c:1", "company_slug": "c", "first_seen": "2026-10-01", "last_seen": "2026-11-10", "title": "t"}
    today_a1 = {"id": "g:a:1", "company_slug": "a", "first_seen": "2026-11-23", "last_seen": "2026-11-23", "title": "t"}
    results = [
        {"slug": "a", "ok": True, "jobs": [today_a1]},      # a:2 vanished -> closed
        {"slug": "b", "ok": False, "jobs": []},             # b failed -> keep yesterday's
        {"slug": "c", "ok": False, "jobs": []},             # c failed for 13 days -> drop
    ]
    out = {j["id"]: j for j in merge([old_a, old_a2, old_b, stale_c], results, now)}
    assert set(out) == {"g:a:1", "g:b:1"}
    assert out["g:a:1"]["first_seen"] == "2026-11-01"       # first_seen survives
    # If every approved feed fails, fresh entries survive briefly but still expire.
    outage = merge([old_b, stale_c], [], now)
    assert [job["id"] for job in outage] == ["g:b:1"]


# ----------------------------------------------------------- company security
from crawler.company_security import is_company_approved, screen_vacancy, validate_vacancy_source
from crawler.company_registry import connect, upsert_candidate, set_domain, approve_source, approved_companies


def test_company_approval_fails_closed():
    company = co()
    assert is_company_approved(company)[0] is True
    del company["verification"]["source"]["approved"]
    assert is_company_approved(company)[0] is False


def test_vacancy_must_match_approved_ats_tenant():
    company = co()
    assert validate_vacancy_source("https://boards.greenhouse.io/acme/jobs/12", company)[0] is True
    assert validate_vacancy_source("https://boards.greenhouse.io/acme-imposter/jobs/12", company)[0] is False
    assert validate_vacancy_source("http://boards.greenhouse.io/acme/jobs/12", company)[0] is False


def test_workable_canonical_short_job_urls_are_accepted_only_from_workable():
    company = co()
    company["ats"] = "workable"
    company["ats_slug"] = "acme"
    assert validate_vacancy_source("https://apply.workable.com/acme/j/ABC123/apply", company)[0] is True
    assert validate_vacancy_source("https://apply.workable.com/j/ABC123", company)[0] is True
    assert validate_vacancy_source("https://apply.workable.com/other/j/ABC123", company)[0] is False
    assert validate_vacancy_source("https://evil.example/j/ABC123", company)[0] is False


def test_manual_trade_and_payment_scam_are_blocked():
    company = co()
    raw = {"title": "Plumber", "url": "https://boards.greenhouse.io/acme/jobs/1", "description": ""}
    assert screen_vacancy(raw, company)[0] == "reject"
    raw = {"title": "Finance Analyst", "url": "https://boards.greenhouse.io/acme/jobs/2",
           "description": "Pay an application fee to process the recruitment."}
    assert screen_vacancy(raw, company)[0] == "reject"
    raw = {"title": "Finance Analyst", "url": "https://boards.greenhouse.io/acme/jobs/3",
           "description": "Contact our recruiter on WhatsApp."}
    assert screen_vacancy(raw, company)[0] == "review"


def test_unclassified_professional_role_is_retained_for_the_other_filter():
    raw = {"id": "1", "title": "Mystery Role", "url": "https://x.test/1", "location": "Paris"}
    job = normalize(raw, co(), NOW)
    assert job is not None
    assert job["role"] == "other"


def test_registry_store_requires_explicit_domain_and_source_approval(tmp_path):
    db = tmp_path / "registry.sqlite3"
    con = connect(db)
    upsert_candidate(con, country="FR", registry_id="123456789", legal_name="Example SAS",
                     registry_status="active", registry_source="INSEE SIRENE", legal_person=True,
                     raw={"employer_scope": "employer", "employee_range": "11"})
    assert approved_companies(db) == []
    set_domain(con, "FR", "123456789", "https://example.fr", confirmed=True)
    # A career source must first exist as a discovered record; no blind slug approval.
    row = con.execute("SELECT id FROM companies WHERE country='FR' AND registry_id='123456789'").fetchone()
    con.execute("INSERT INTO career_sources (company_id,ats,ats_slug,career_url,evidence_url,evidence_type) VALUES (?,?,?,?,?,?)",
                (row["id"], "greenhouse", "example", "https://example.fr/careers", "https://example.fr", "linked_from_verified_company_domain"))
    con.commit()
    approve_source(con, "FR", "123456789", "greenhouse", "example", "https://example.fr/careers", "linked_from_verified_company_domain")
    approved = approved_companies(db)
    assert len(approved) == 1
    assert approved[0]["verification"]["registry"]["legal_person"] is True
    con.close()


def test_workday_detection_adapter_and_vacancy_source_validation():
    html = '<a href="https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite">Careers</a>'
    hits = discover.detect(html)
    slug = next(slug for ats, slug in hits if ats == "workday")
    assert slug == "nvidia|wd5|NVIDIAExternalCareerSite|en-US"

    endpoint = "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs"
    http = FakeHttp({endpoint: {"total": 1, "jobPostings": [{
        "title": "Senior Finance Analyst", "externalPath": "/job/Brussels/Senior-Finance-Analyst_JR-123",
        "locationsText": "Brussels, Belgium", "postedOn": "Posted Today"
    }]}})
    jobs = adapters.workday(slug, http)
    assert len(jobs) == 1
    assert jobs[0]["url"] == "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/Brussels/Senior-Finance-Analyst_JR-123"
    company = co(ats="workday", slug=slug)
    assert screen_vacancy(jobs[0], company)[0] == "publish"
    assert screen_vacancy({**jobs[0], "url": "https://fake.example/jobs/1"}, company)[0] == "reject"


def test_crawl_blocks_unverified_company_before_network():
    company = co()
    company["verification"]["status"] = "pending"
    result = crawl_company(company, FakeHttp({}), NOW)
    assert result["ok"] is False and "publication gate blocked" in result["error"]


def test_registry_search_importers_use_active_legal_entities(monkeypatch, tmp_path):
    from crawler import company_registry as registry

    class RegistryHttp:
        def get_json(self, url, **kwargs):
            if url.endswith("/siren"):
                return {"unitesLegales": [
                    {"uniteLegale": {"siren": "123456789", "denominationUniteLegale": "Example SAS",
                                     "etatAdministratifUniteLegale": "A", "categorieJuridiqueUniteLegale": "5720",
                                     "statutDiffusionUniteLegale": "O"}},
                    {"uniteLegale": {"siren": "987654321", "denominationUniteLegale": "Inactive",
                                     "etatAdministratifUniteLegale": "C", "categorieJuridiqueUniteLegale": "5720"}},
                    {"uniteLegale": {"siren": "111111111", "denominationUniteLegale": "Sole trader",
                                     "etatAdministratifUniteLegale": "A", "categorieJuridiqueUniteLegale": "1000"}},
                    {"uniteLegale": {"siren": "333333333", "denominationUniteLegale": "Undivided grouping",
                                     "etatAdministratifUniteLegale": "A", "categorieJuridiqueUniteLegale": "2110"}},
                    {"uniteLegale": {"siren": "222222222", "denominationUniteLegale": "Partial diffusion",
                                     "etatAdministratifUniteLegale": "A", "categorieJuridiqueUniteLegale": "5720",
                                     "statutDiffusionUniteLegale": "P"}},
                ]}
            if url == registry.KVK_SEARCH:
                return {"resultaten": [{"kvkNummer": "12345678", "type": "rechtspersoon", "actief": "Ja", "naam": "Example BV"}]}
            if url == registry.KVK_PROFILE:
                return {"statutaireNaam": "Example BV", "rechtsvorm": "Besloten Vennootschap",
                        "websites": [{"website": "https://www.example.nl"}],
                        "sbiActiviteiten": [{"sbiCode": "6201"}]}
            raise AssertionError(url)

    monkeypatch.setenv("INSEE_SIRENE_API_KEY", "test-key")
    fr = registry._insee_companies("denominationUniteLegale:EXAMPLE", 100, RegistryHttp())
    assert len(fr) == 2
    active = next(item for item in fr if item["registry_id"] == "123456789")
    inactive = next(item for item in fr if item["registry_id"] == "987654321")
    assert active["legal_person"] is True and active["registry_status"] == "active"
    assert inactive["registry_status"] == "inactive"

    monkeypatch.setenv("KVK_API_KEY", "test-key")
    nl = registry._kvk_companies("Example", 100, RegistryHttp())
    assert len(nl) == 1 and nl[0]["official_domain"] == "https://www.example.nl"


def test_cbe_import_requires_mapping_and_skips_non_legal_people(tmp_path):
    from crawler.company_registry import connect, import_cbe_file
    csv_file = tmp_path / "cbe.csv"
    csv_file.write_text(
        "EnterpriseNumber;Name;Status;LegalForm;PersonType\n"
        "0123456789;Example SA;AC;SA;LEGAL\n"
        "0987654321;Individual;AC;PERSON;NATURAL\n"
        "1111111111;Closed SA;CE;SA;LEGAL\n",
        encoding="utf-8",
    )
    mapping_file = tmp_path / "mapping.json"
    mapping_file.write_text(json.dumps({
        "number_column": "EnterpriseNumber", "name_column": "Name", "status_column": "Status",
        "active_values": ["AC"], "legal_form_column": "LegalForm",
        "legal_person_column": "PersonType", "legal_person_values": ["LEGAL"],
        "snapshot_date": datetime.now(timezone.utc).date().isoformat(),
    }), encoding="utf-8")
    con = connect(tmp_path / "registry.sqlite3")
    assert import_cbe_file(con, csv_file, mapping_file) == 1
    row = con.execute("SELECT registry_id,legal_person,registry_status FROM companies").fetchone()
    assert tuple(row) == ("0123456789", 1, "active")
    con.close()


def test_domain_change_revokes_previously_approved_career_source(tmp_path):
    from crawler.company_registry import connect, upsert_candidate, set_domain, approve_source, approved_companies
    db = tmp_path / "registry.sqlite3"
    con = connect(db)
    upsert_candidate(con, country="NL", registry_id="12345678", legal_name="Example BV",
                     registry_status="active", registry_source="KVK", legal_person=True,
                     official_domain="https://old.example.nl", domain_verified=True,
                     domain_method="registry_registered_website")
    row = con.execute("SELECT id FROM companies").fetchone()
    con.execute("INSERT INTO career_sources (company_id,ats,ats_slug,career_url,evidence_url,evidence_type) VALUES (?,?,?,?,?,?)",
                (row["id"], "greenhouse", "example", "https://old.example.nl/careers", "https://old.example.nl", "linked_from_verified_company_domain"))
    con.commit()
    approve_source(con, "NL", "12345678", "greenhouse", "example", "https://old.example.nl/careers", "linked_from_verified_company_domain")
    assert len(approved_companies(db)) == 1
    upsert_candidate(con, country="NL", registry_id="12345678", legal_name="Example BV Updated",
                     registry_status="active", registry_source="KVK", legal_person=True,
                     official_domain="https://new.example.nl", domain_verified=False)
    assert approved_companies(db) == []
    con.close()



def test_registry_staleness_blocks_publication():
    from crawler.company_security import is_company_approved
    company = co()
    company["verification"]["registry"]["checked_at"] = "2025-01-01"
    ok, reason = is_company_approved(company)
    assert ok is False and "older than 90 days" in reason


def test_stale_domain_or_career_source_check_blocks_publication():
    from crawler.company_security import is_company_approved
    company = co()
    company["verification"]["domain"]["checked_at"] = "2025-01-01"
    ok, reason = is_company_approved(company)
    assert ok is False and "official-domain check is older" in reason
    company = co()
    company["verification"]["source"]["checked_at"] = "2025-01-01"
    ok, reason = is_company_approved(company)
    assert ok is False and "career-source check is older" in reason


def test_manual_trade_exclusion_works_in_belgian_languages():
    company = co()
    for title in ("Plombier", "Schoonmaker", "Klempner", "Livreur", "Loodgieter"):
        raw = {"title": title, "url": "https://boards.greenhouse.io/acme/jobs/1", "description": ""}
        assert screen_vacancy(raw, company)[0] == "reject", title


def test_official_linked_ats_source_is_auto_approved_and_custom_page_waits(tmp_path):
    from crawler.company_registry import discover_company_sources
    db = tmp_path / "registry.sqlite3"
    con = connect(db)
    upsert_candidate(con, country="FR", registry_id="123456789", legal_name="Example SAS",
                     registry_status="active", registry_source="INSEE SIRENE", legal_person=True,
                     raw={"employer_scope": "employer", "employee_range": "11"})
    set_domain(con, "FR", "123456789", "https://example.fr", confirmed=True)
    homepage = '<html><a href="https://example.fr/careers">Careers</a></html>'
    careers = '<html><iframe src="https://boards.greenhouse.io/example"></iframe></html>'
    found = discover_company_sources(con, "FR", "123456789", FakeHttp({
        "https://example.fr": homepage, "https://example.fr/careers": careers}))
    assert len(found) == 1 and found[0]["auto_approve"] is True
    approved = approved_companies(db)
    assert len(approved) == 1 and approved[0]["ats_slug"] == "example"
    assert is_company_approved(approved[0])[0] is True

    # A custom first-party page with structured jobs but no known ATS is
    # discovered as a candidate and cannot be automatically published.
    upsert_candidate(con, country="FR", registry_id="987654321", legal_name="Custom SAS",
                     registry_status="active", registry_source="INSEE SIRENE", legal_person=True,
                     raw={"employer_scope": "employer", "employee_range": "11"})
    set_domain(con, "FR", "987654321", "https://custom.fr", confirmed=True)
    custom_home = '<html><a href="https://custom.fr/jobs">Careers</a></html>'
    custom_jobs = '<html><script type="application/ld+json">{"@type":"JobPosting","title":"Finance Analyst"}</script></html>'
    candidates = discover_company_sources(con, "FR", "987654321", FakeHttp({
        "https://custom.fr": custom_home, "https://custom.fr/jobs": custom_jobs}))
    assert len(candidates) == 1 and candidates[0]["ats"] == "jsonld"
    assert candidates[0].get("auto_approve") is not True
    assert len(approved_companies(db)) == 1
    con.close()


def test_discovery_revokes_career_source_removed_from_official_company_site(tmp_path):
    from crawler.company_registry import discover_company_sources
    db = tmp_path / "registry.sqlite3"
    con = connect(db)
    upsert_candidate(con, country="FR", registry_id="123456789", legal_name="Example SAS",
                     registry_status="active", registry_source="INSEE SIRENE", legal_person=True,
                     raw={"employer_scope": "employer", "employee_range": "11"})
    set_domain(con, "FR", "123456789", "https://example.fr", confirmed=True)
    discover_company_sources(con, "FR", "123456789", FakeHttp({
        "https://example.fr": '<a href="https://example.fr/careers">Careers</a>',
        "https://example.fr/careers": '<iframe src="https://boards.greenhouse.io/example"></iframe>',
    }))
    assert len(approved_companies(db)) == 1
    # A fresh re-check of the verified site finds no longer any career/ATS link.
    discover_company_sources(con, "FR", "123456789", FakeHttp({"https://example.fr": "<html>No careers link</html>"}))
    assert approved_companies(db) == []
    status = con.execute("SELECT last_status FROM career_sources").fetchone()[0]
    assert status == "no_longer_linked_from_verified_company_site"
    con.close()



def test_discover_all_batch_progresses_past_companies_without_career_pages(tmp_path):
    from crawler.company_registry import connect, upsert_candidate, set_domain, discover_all_sources
    db = tmp_path / "registry.sqlite3"
    con = connect(db)
    for registry_id, name, domain in [
        ("111111111", "A No Careers", "https://a-nocareers.example"),
        ("222222222", "B Has Careers", "https://b-careers.example"),
    ]:
        upsert_candidate(con, country="FR", registry_id=registry_id, legal_name=name,
                         registry_status="active", registry_source="INSEE SIRENE", legal_person=True,
                         raw={"employer_scope": "employer", "employee_range": "11"})
        set_domain(con, "FR", registry_id, domain, confirmed=True)
    routes = {
        "https://a-nocareers.example": "<html><body>Company information only</body></html>",
        "https://b-careers.example": '<html><a href="https://b-careers.example/careers">Careers</a></html>',
        "https://b-careers.example/careers": '<html><iframe src="https://boards.greenhouse.io/bcareers"></iframe></html>',
    }
    http = FakeHttp(routes)
    visited_1, sources_1 = discover_all_sources(con, http, country="FR", limit=1)
    assert (visited_1, sources_1) == (1, 0)
    first = con.execute("SELECT career_discovery_status FROM companies WHERE registry_id='111111111'").fetchone()[0]
    assert first == "no_career_source_found"

    # The first company is now recorded as checked, so the next bounded batch
    # progresses to the next company rather than repeatedly selecting it.
    visited_2, sources_2 = discover_all_sources(con, http, country="FR", limit=1)
    assert (visited_2, sources_2) == (1, 1)
    second = con.execute("SELECT career_discovery_status FROM companies WHERE registry_id='222222222'").fetchone()[0]
    assert second == "sources_found"
    con.close()


def test_failed_career_site_attempt_does_not_block_next_company_batch(tmp_path):
    from crawler.company_registry import connect, upsert_candidate, set_domain, discover_all_sources
    db = tmp_path / "registry.sqlite3"
    con = connect(db)
    for registry_id, name, domain in [
        ("111111111", "A Unreachable", "https://a-unreachable.example"),
        ("222222222", "B Careers", "https://b-careers.example"),
    ]:
        upsert_candidate(con, country="FR", registry_id=registry_id, legal_name=name,
                         registry_status="active", registry_source="INSEE SIRENE", legal_person=True,
                         raw={"employer_scope": "employer", "employee_range": "11"})
        set_domain(con, "FR", registry_id, domain, confirmed=True)
    http = FakeHttp({
        "https://b-careers.example": '<html><a href="https://b-careers.example/careers">Careers</a></html>',
        "https://b-careers.example/careers": '<html><iframe src="https://boards.greenhouse.io/bcareers"></iframe></html>',
    })
    visited_1, sources_1 = discover_all_sources(con, http, country="FR", limit=1)
    assert (visited_1, sources_1) == (1, 0)
    failed = con.execute("SELECT career_discovery_attempted_at,career_discovery_checked_at,career_discovery_status FROM companies WHERE registry_id='111111111'").fetchone()
    assert failed[0] and failed[1] is None and str(failed[2]).startswith("failed:")

    # A recent failed attempt is put on a seven-day retry delay, allowing the
    # next company in the ordered queue to be discovered immediately.
    visited_2, sources_2 = discover_all_sources(con, http, country="FR", limit=1)
    assert (visited_2, sources_2) == (1, 1)
    con.close()

def test_kvk_connector_excludes_foundations_and_unknown_forms(monkeypatch):
    from crawler import company_registry as registry
    monkeypatch.setenv("KVK_API_KEY", "test-key")

    class KvkHttp:
        def get_json(self, url, **kwargs):
            if url == registry.KVK_SEARCH:
                return {"resultaten": [
                    {"kvkNummer": "12345678", "type": "rechtspersoon", "actief": "Ja", "naam": "Example BV"},
                    {"kvkNummer": "23456789", "type": "rechtspersoon", "actief": "Ja", "naam": "Example Foundation"},
                ]}
            number = (kwargs.get("params") or {}).get("kvkNummer")
            if url == registry.KVK_PROFILE and number == "12345678":
                return {"statutaireNaam": "Example BV", "rechtsvorm": "Besloten Vennootschap"}
            if url == registry.KVK_PROFILE and number == "23456789":
                return {"statutaireNaam": "Example Foundation", "rechtsvorm": "Stichting"}
            raise AssertionError((url, kwargs))

    results = registry._kvk_companies("Example", 100, KvkHttp())
    assert [item["registry_id"] for item in results] == ["12345678"]


def test_inspect_cbe_codes_reads_requested_categories(tmp_path):
    import zipfile
    from crawler.company_registry import inspect_cbe_codes
    archive = tmp_path / "cbe.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("code.csv", "Category,Code,Language,Description\nJuridicalForm,015,EN,Private limited company\nJuridicalForm,017,EN,Non-profit association\nContactType,WEB,EN,Internet address\n")
    rows = inspect_cbe_codes(archive, ["JuridicalForm"], "EN")
    assert [(item["code"], item["description"]) for item in rows] == [
        ("015", "Private limited company"), ("017", "Non-profit association")]


def test_cbe_full_zip_import_joins_names_and_registered_websites(tmp_path):
    import csv
    import zipfile
    from crawler.company_registry import connect, import_cbe_file

    archive = tmp_path / "KboOpenData_1_2026_11_Full.zip"
    members = {
        "meta.csv": "Variable,Value\nSnapshotDate,2026-11-20\nExtractType,Full\n",
        "code.csv": "Category,Code,Language,Description\nTypeOfDenomination,LEGAL,EN,Official name\nContactType,WEB,EN,Internet address\n",
        "enterprise.csv": "EnterpriseNumber,Status,JuridicalSituation,TypeOfEnterprise,JuridicalForm\n0123.456.789,AC,000,ELP,001\n0987.654.321,AC,000,EPP,\n0111.222.333,AC,000,ELP,017\n",
        "denomination.csv": "EntityNumber,Language,TypeOfDenomination,Denomination\n0123.456.789,NL,LEGAL,Voorbeeld BV\n0123.456.789,EN,LEGAL,Example BV\n0987.654.321,EN,LEGAL,Person\n0111.222.333,EN,LEGAL,Example nonprofit\n",
        "contact.csv": "EntityNumber,EntityContact,ContactType,Value\n0123.456.789,ENT,WEB,https://www.example.be\n",
    }
    with zipfile.ZipFile(archive, "w") as zf:
        # Deliberately write code.csv first: importer must select named members.
        for name, body in members.items():
            zf.writestr(name, body)

    mapping = tmp_path / "cbe-map.json"
    mapping.write_text(json.dumps({
        "enterprise_file": "enterprise.csv", "denomination_file": "denomination.csv", "contact_file": "contact.csv",
        "number_column": "EnterpriseNumber", "status_column": "Status", "active_values": ["AC"],
        "legal_form_column": "JuridicalForm", "legal_person_column": "TypeOfEnterprise", "legal_person_values": ["ELP"],
        "denomination_entity_column": "EntityNumber", "denomination_type_column": "TypeOfDenomination",
        "denomination_value_column": "Denomination", "denomination_language_column": "Language",
        "legal_name_type_values": ["LEGAL"], "preferred_languages": ["EN", "FR", "NL", "DE"],
        "allowed_legal_forms": ["001"],
        "contact_entity_column": "EntityNumber", "contact_type_column": "ContactType",
        "contact_value_column": "Value", "website_contact_type_values": ["WEB"],
        "full_snapshot": True
    }), encoding="utf-8")

    con = connect(tmp_path / "registry.sqlite3")
    try:
        try:
            import_cbe_file(con, archive, mapping, confirm_full_snapshot=False)
            assert False, "Full snapshots must require explicit confirmation"
        except ValueError as exc:
            assert "confirm-full-snapshot" in str(exc)
        assert import_cbe_file(con, archive, mapping, confirm_full_snapshot=True) == 1
        row = con.execute("SELECT registry_id,legal_name,legal_person,registry_status,official_domain,domain_verified,registry_checked_at FROM companies").fetchone()
        assert tuple(row) == ("0123456789", "Example BV", 1, "active", "www.example.be", 1, "2026-11-20")
    finally:
        con.close()


def test_insee_stock_import_streams_and_filters_non_legal_people(tmp_path):
    from crawler.company_registry import import_fr_stock_file
    csv_file = tmp_path / "stock-unite-legale.csv"
    csv_file.write_text(
        "siren;denominationUniteLegale;etatAdministratifUniteLegale;categorieJuridiqueUniteLegale;statutDiffusionUniteLegale;activitePrincipaleUniteLegale;trancheEffectifsUniteLegale\n"
        "123456789;Example SAS;A;5720;O;62.01Z;11\n"
        "987654321;Closed SAS;C;5720;O;62.01Z;00\n"
        "111111111;Individual;A;1000;O;62.01Z;NN\n"
        "222222222;Partial diffusion;A;5720;P;62.01Z;11\n"
        "333333333;Undivided grouping;A;2110;O;62.01Z;11\n",
        encoding="utf-8",
    )
    mapping_file = tmp_path / "insee-mapping.json"
    mapping_file.write_text(json.dumps({
        "number_column": "siren", "name_column": "denominationUniteLegale",
        "status_column": "etatAdministratifUniteLegale", "active_values": ["A"],
        "inactive_values": ["C"], "legal_form_column": "categorieJuridiqueUniteLegale",
        "legal_form_prefixes": ["3", "4", "5", "6"],
        "employee_range_column": "trancheEffectifsUniteLegale",
        "diffusion_column": "statutDiffusionUniteLegale", "excluded_diffusion_values": ["P", "N"],
        "snapshot_date": datetime.now(timezone.utc).date().isoformat(),
        "activity_column": "activitePrincipaleUniteLegale"
    }), encoding="utf-8")
    con = connect(tmp_path / "registry.sqlite3")
    assert import_fr_stock_file(con, csv_file, mapping_file) == 2
    rows = con.execute("SELECT registry_id,registry_status FROM companies ORDER BY registry_id").fetchall()
    assert [tuple(row) for row in rows] == [("123456789", "active"), ("987654321", "inactive")]
    con.close()


def test_approved_snapshot_export_and_loader(tmp_path):
    from crawler.company_registry import export_approved
    from crawler.crawl import load_companies
    db = tmp_path / "registry.sqlite3"
    con = connect(db)
    upsert_candidate(con, country="NL", registry_id="12345678", legal_name="Example BV",
                     registry_status="active", registry_source="KVK", legal_person=True,
                     official_domain="https://example.nl", domain_verified=True,
                     domain_method="registry_registered_website")
    row = con.execute("SELECT id FROM companies").fetchone()
    con.execute("INSERT INTO career_sources (company_id,ats,ats_slug,career_url,evidence_url,evidence_type,approved,approval_method,approved_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (row["id"], "greenhouse", "example", "https://boards.greenhouse.io/example", "https://example.nl",
                 "linked_from_verified_company_domain", 1, "linked_from_verified_company_domain", datetime.now(timezone.utc).isoformat()))
    con.commit()
    snapshot = tmp_path / "approved.json"
    assert export_approved(db, snapshot) == 1
    con.close()
    loaded = load_companies(tmp_path / "legacy.yaml", registry_db=tmp_path / "missing.sqlite3", approved_file=snapshot)
    assert len(loaded) == 1 and loaded[0]["ats_slug"] == "example"


def test_no_approved_sources_fails_closed_and_preserves_public_job_dataset(tmp_path):
    from crawler.crawl import main as crawl_main
    out = tmp_path / "site-data"
    out.mkdir()
    original = {"demo": False, "jobs": [{"id": "old", "company_slug": "previously-approved"}]}
    (out / "jobs.json").write_text(json.dumps(original), encoding="utf-8")
    approved_file = tmp_path / "approved.json"
    approved_file.write_text(json.dumps({"companies": []}), encoding="utf-8")
    code = crawl_main(["--registry-db", str(tmp_path / "does-not-exist.sqlite3"),
                       "--approved-companies", str(approved_file), "--out", str(out),
                       "--review-out", str(tmp_path / "review.json")])
    payload = json.loads((out / "jobs.json").read_text(encoding="utf-8"))
    assert code == 2 and payload == original


def test_france_launch_seed_is_approved_and_not_a_company_limit():
    from pathlib import Path
    from crawler.crawl import load_companies

    companies = load_companies(Path("companies.yaml"), curated_config=True, country="FR")
    assert len(companies) == 1
    assert companies[0]["name"] == "Hugging Face"
    assert companies[0]["hq"] == "FR"
    assert is_company_approved(companies[0]) == (True, None)
    assert "registry" not in companies[0]["verification"]


def test_country_scope_excludes_foreign_curated_sources(tmp_path):
    from crawler.crawl import load_companies

    path = tmp_path / "companies.yaml"
    path.write_text("""companies:
  - name: France Co
    slug: france-co
    ats: greenhouse
    hq: FR
    official_domain: france.example
    official_careers_url: https://france.example/careers
    career_url: https://boards.greenhouse.io/france-co
    source_reviewed_on: '2026-11-23'
  - name: Foreign Co
    slug: foreign-co
    ats: greenhouse
    hq: DE
    official_domain: foreign.example
    official_careers_url: https://foreign.example/careers
    career_url: https://boards.greenhouse.io/foreign-co
    source_reviewed_on: '2026-11-23'
""", encoding="utf-8")
    companies = load_companies(path, curated_config=True, country="FR")
    assert [company["name"] for company in companies] == ["France Co"]


def test_total_live_feed_outage_keeps_previous_published_jobs(tmp_path, monkeypatch):
    from crawler import crawl as crawl_module

    companies_file = tmp_path / "companies.yaml"
    companies_file.write_text("""companies:
  - name: Acme
    slug: acme
    ats: greenhouse
    hq: DE
    official_domain: acme.example
    official_careers_url: https://acme.example/careers
    career_url: https://boards.greenhouse.io/acme
    source_reviewed_on: '2026-11-23'
""", encoding="utf-8")
    out = tmp_path / "site-data"
    out.mkdir()
    original = {"generated_at": "2026-11-22T05:15:00+00:00", "count": 1, "demo": False,
                "jobs": [{"id": "greenhouse:acme:old", "company_slug": "acme", "url": "https://boards.greenhouse.io/acme/jobs/1"}]}
    jobs_path = out / "jobs.json"
    jobs_path.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setattr(crawl_module, "Http", lambda **kwargs: FakeHttp({}))

    code = crawl_module.main(["--curated-config", "--country", "DE", "--companies", str(companies_file),
                              "--registry-db", str(tmp_path / "missing.sqlite3"),
                              "--approved-companies", str(tmp_path / "missing.json"),
                              "--out", str(out), "--review-out", str(tmp_path / "review.json")])

    assert code == 2
    assert json.loads(jobs_path.read_text(encoding="utf-8")) == original


def test_release_contains_only_real_non_demo_job_feed():
    from pathlib import Path
    payload = json.loads(Path("site/data/jobs.json").read_text(encoding="utf-8"))
    assert payload["demo"] is False
    assert isinstance(payload["jobs"], list)
    assert payload["count"] == len(payload["jobs"])
    # Live crawls populate this file; reject the old fictional demo employer.
    assert all("Northwind Labs" not in str(job.get("company", "")) for job in payload["jobs"])
