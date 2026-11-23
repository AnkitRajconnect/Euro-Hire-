# Euro-Hire France

A France-first job index built from public applicant-tracking-system career boards. The pipeline discovers ATS boards first, reads current vacancies, keeps roles actually based in France, and then matches the hiring company against the French public Recherche d'Entreprises API to add SIREN and NAF activity codes.

## Why ATS-first

The crawler does not start from millions of registered businesses. It starts with companies that already publish vacancies through a public careers feed. This avoids spending most of the crawl budget on inactive businesses that are not hiring.

### Weekly discovery

1. Seed public ATS board tokens from the MIT-licensed Open ATS Feed snapshot (Greenhouse, Ashby, Lever, Breezy, Recruitee and Teamtailor).
2. Expand the candidate index from Common Crawl's public S3 cluster.idx files using byte-range requests. The CDX query API has been unreliable under bulk load, so it is not on the critical path.
3. Add discovered board IDs to data/jobs.db.
4. Probe each board through its public, unauthenticated listing endpoint.
5. Keep only jobs with clear France evidence: ISO country code FR, France in the location, or a recognised French city or region.
6. Create company rows only after a board has at least one French vacancy. Then search the public French Recherche d'Entreprises API and store a high-confidence SIREN and NAF match.

### Daily sync

The daily workflow revisits only ATS boards that have already produced French vacancies. A missing job is expired only after that board returns a complete, successful response. A failed feed does not close jobs. Welcome to the Jungle is queried as a separate, France-filtered public search index; its complete result must succeed before old WTTJ listings are expired.

## Public ATS sources

| Source | Read method | Status |
|---|---|---|
| Greenhouse | Public Job Board API | Included |
| Lever | Public Postings API, including EU host | Included; rate limited to one request per second |
| Ashby | Public Job Postings API | Included |
| Workable | Public careers widget | Included; deliberately low concurrency |
| Recruitee | Public offers feed | Included until the announced token change on 10 February 2027 |
| Teamtailor | Public RSS careers feed | Included |
| Breezy HR | Public careers JSON | Included |
| Taleez | Public careers-site JSON route | Experimental; separate from the official API, which requires a public key |
| Welcome to the Jungle | Public frontend search index, country-filtered to France | Experimental; not a documented third-party API |
| SmartRecruiters | Public postings API, filtered to France | Included behind a live robots.txt permission check; skipped whenever the API host disallows the endpoint |

Personio's historical tenant robots policy disallows its XML feed, so it is also excluded. A provider being popular in Europe does not make every endpoint free or permitted. This project does not use private recruiter APIs, employer tokens, paid job-data APIs, applicant records, personal emails or phone numbers.

The source index is not a promise to support every ATS that exists. Only providers with a public feed and a usable, permission-compatible read path are enabled. A provider's public feed can change or be disabled, and each failure is reported separately.

## INSEE enrichment

After a company has produced a French vacancy, Euro-Hire searches the public API:

https://recherche-entreprises.api.gouv.fr/search

The public search API is rate-limited to seven requests per second. Euro-Hire intentionally uses a lower rate. Matches are accepted only when the result is active (etat_administratif = A), diffusible (statut_diffusion = O), not an individual legal form (nature_juridique != 1000), and has a high name-match score. Ambiguous names are left unmatched rather than assigned the wrong SIREN.

The database stores the SIREN, legal name, legal city, legal form, NAF 2025 code and (when supplied) legacy NAF Rev2 code. Personal contact fields are not requested or stored. Companies without a safe match remain unclassified; their actual job listings are not discarded.

## Database and exports

- data/jobs.db — SQLite source of truth: discovered boards, hiring companies, active/closed jobs and crawl state.
- data/active_jobs.json — full export with jobs grouped by NAF code.
- data/active_jobs.csv — flat spreadsheet-friendly export.
- data/pipeline_report.json — provider counts, feed success/failure, active jobs and INSEE match totals.
- site/data/jobs.json — frontend payload used by GitHub Pages.

Job rows contain public vacancy details and direct application links. They do not contain candidate or employee personal data.

## Run locally

    python -m pip install -r requirements.txt
    python -m pytest -q
    python discover_companies.py
    python sync_jobs.py

The first discovery run takes longer because it seeds the ATS index and harvests Common Crawl. Daily sync then checks only discovered boards that have already yielded French vacancies. Both scripts can be run manually from GitHub Actions.

## Limitations

Common Crawl is an archive of pages it has previously indexed, not a complete directory of every ATS customer. The public token snapshot is dated and used only as a source of candidate board IDs. A candidate is not a company record until its live feed returns jobs in France. New, custom-domain, blocked or unindexed boards can be missed. WTTJ's public frontend search implementation may change without notice. No public pipeline can honestly guarantee every French job or every ATS customer.
