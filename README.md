# Euro-Hire

Euro-Hire discovers public ATS career boards, identifies boards with live jobs in France, enriches company identities against the open French government company-search API, and refreshes French vacancies daily.

## Two-stage pipeline

### 1. Weekly company discovery

discover_companies.py runs every Sunday at 02:00 UTC.

- Starts with a public, MIT-licensed Common Crawl measurement snapshot and expands candidates from Common Crawl's free S3-hosted cluster.idx files using HTTP byte ranges.
- It does not use the unstable Common Crawl CDX query API for bulk discovery.
- Every candidate slug is re-probed against the employer's live public job feed. A source is stored only when its current board contains at least one France-based role.
- Each board display name is searched against the official API Recherche d'entreprises. A SIREN is linked only when a single exact normalized name match is found.
- If a brand or trading name cannot be matched safely, its public jobs are still retained with a nullable SIREN. No legal identity is guessed.

### 2. Daily job sync

sync_jobs.py runs every day at 06:00 UTC.

- Reads the discovered ATS sources from data/jobs.db.
- Refreshes each public board asynchronously with httpx.
- Keeps current France-based jobs and stores only the job fields needed by the site.
- Removes personal email addresses and phone numbers from job descriptions before persistence.
- Expires a job only after its own source board was successfully fetched and no longer returned it. A 403, 429, 5xx, timeout or other failed source does not expire its jobs.
- If every feed fails, the previous JSON and CSV exports are preserved.

## Public ATS routes

These are anonymous public job-board reads, not private recruiting APIs:

| Provider | Public route | Notes |
|---|---|---|
| Greenhouse | Job Board API | Public published jobs |
| Lever | Postings API | 1 request/second crawl delay respected |
| Ashby | Public Job Posting API | Published jobs |
| Workable | Public careers widget | Lower request rate to avoid throttling |
| Breezy HR | Public /json board | Public career-board response |
| Recruitee | Public offers endpoint | Automatically disabled on 10 February 2027 when the published token requirement takes effect |
| Teamtailor | Public jobs RSS | RSS feed only |
| Gem | Public Job Board API | Job reads are public; application submission is never called |
| Pinpoint | Public postings.json | Documented public jobs endpoint |
| BambooHR | Public careers list | Employer-hosted career-board endpoint |
| Rippling | Public hosted-board jobs feed | No private Recruiting API credentials |
| Workday | Public career-site search endpoint | Tenant robots.txt checked before each board is read |

SmartRecruiters and Personio are deliberately excluded. Independent live and archived robots checks found the relevant public feed paths disallowed. Private ATS APIs, paid data providers, employer account tokens, OAuth and candidate-management endpoints are not used.

Workday's endpoint is an undocumented public career-site endpoint, not a private HCM API. It is only called after the exact tenant robots.txt path has been checked and allowed. The upstream open-ats-feed snapshot is a candidate source only. Its board slugs and counts are re-verified against live feeds and are not copied into Euro-Hire source code. Attribution and the upstream MIT licence are retained here: https://github.com/veresk06/open-ats-feed/blob/main/LICENSE.

## French government data and privacy

The government API allows a maximum of 7 requests/second per IP. Euro-Hire uses a 5 requests/second limiter and sends a descriptive User-Agent.

A legal entity is linked only when all of these checks pass:
- etat_administratif is A
- nature_juridique is not 1000
- statut_diffusion is O
- Exactly one returned legal name, full name, commercial name or acronym matches the board's normalized display name.

The government API already excludes non-diffusible entities; the explicit status check is retained as an additional safeguard. The pipeline never reads or stores dirigeants, personal emails or telephone fields. Public job descriptions are cleaned of email addresses and phone numbers before they are saved.

Only verified legal entities enter the companies table, whose primary key is SIREN. An unmatched ATS brand remains in ats_sources with a nullable company_siren; its public jobs can still be shown without pretending the brand is a verified legal entity.

## Database and exports

- data/jobs.db: SQLite companies, ATS sources and jobs.
- data/active_jobs.json: active France jobs with SIREN and NAF fields where a legal match exists.
- data/active_jobs.csv: flat export.
- site/data/jobs.json: frontend-compatible export used by the existing Euro-Hire website.
- data/discovery_report.json and data/sync_report.json: coverage and sync outcomes.

The companies table uses SIREN as its primary key and contains only active, fully diffusible legal companies that pass exact name matching. ATS sources hold a nullable foreign key to companies.siren. Boards that cannot be safely matched remain in ats_sources and can still contribute jobs; multiple boards with the same matched SIREN share one company row.

Both workflows use the same GitHub Actions concurrency group so weekly and daily jobs cannot write the SQLite database at the same time.

## Run locally

    python -m pip install -r requirements.txt
    python -m pytest -q
    python discover_companies.py --db data/jobs.db --report data/discovery_report.json
    python sync_jobs.py --db data/jobs.db --output-dir data --report data/sync_report.json

For a quick discovery smoke test that skips the Common Crawl S3 refresh:

    python discover_companies.py --seed-only --max-candidates 30

There are no API keys, paid subscriptions or cloud database credentials in this pipeline.

## Coverage limits

Common Crawl is a discoverability floor, not a census of all employers. It can miss new or unlinked boards, custom-domain career pages and providers without an anonymous public listing route. The public candidate snapshot is one starting source, and weekly Common Crawl range-index scans add more board tokens. Every candidate is checked live before it enters the France database.
