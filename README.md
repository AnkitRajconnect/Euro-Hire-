# Euro-Hire

A Europe-wide job discovery site that reads public employer ATS feeds and displays current vacancies on a searchable list and map.

## No employer whitelist

The crawler has no fixed employer-name list and no seven-company cap. A weekly discovery task queries the free Common Crawl URL index for public career-board URLs, identifies board IDs by ATS provider, and writes the results to site/data/boards.json. The crawler then calls each discovered board's public job feed.

The Common Crawl index is a free discovery source. Job details come from the employer's own live public ATS feed.

## Public board adapters

The code supports public, unauthenticated job-listing routes for:

- Greenhouse Job Board API
- Lever Postings API, including the EU host
- Ashby Public Job Postings API
- SmartRecruiters public postings
- Workable public careers widget
- Personio XML feed, when the employer has enabled it
- Teamtailor public RSS
- Workday public career-site search endpoint
- Recruitee public offers route (automatically disabled from 10 February 2027 because the vendor requires an employer token)
- Pinpoint public postings.json
- BambooHR public careers list
- JazzHR public XML job export
- Breezy HR public careers JSON
- Keka public career portal feed
- Homerun public Atom feed
- Gem Job Board API: public job-post reads; application submission is not used
- Rippling public hosted-board jobs feed

No paid job-data provider is used. Private ATS APIs that require employer credentials, a paid account or OAuth approval are excluded. A public feed can still be disabled by an employer, and the provider can change the interface. Failed feeds are reported independently instead of blocking other companies.

## Discovery and refresh

1. Read the latest Common Crawl collection metadata.
2. Search the URL index for the supported ATS board URL patterns.
3. Extract and deduplicate public board IDs, retaining previous discoveries.
4. Crawl a rotating daily batch (up to 5,000 boards), prioritising boards not checked recently.
5. Save the board index, current jobs and a per-provider report.

The index is refreshed weekly and the job feeds are checked daily. The website contains no hard-coded employer list.

## Location and publication

- Physical jobs must resolve to a European country or city.
- Explicit Remote Europe roles are mapped to a Europe-wide marker.
- Generic remote jobs are accepted only for employers whose headquarters are known to be in Europe.
- Job URLs must use HTTPS. URLs returned directly by a validated public ATS feed may use the employer's own application host; untrusted URLs must match the configured ATS board.
- Manual/service roles and payment scams are rejected. Suspicious recruitment language is held for review.
- Closed jobs disappear after their board is successfully checked. Jobs from boards not yet reached in the current rotation are retained.
- If every checked feed fails, the last successful jobs file is preserved.

## Run locally

    python -m pip install -r requirements.txt
    python -m pytest -q
    python -m crawler.source_discovery --out site/data/boards.json --force
    python -m crawler.crawl --boards site/data/boards.json --out site/data
    node --check site/app.js

No API keys, paid job-data subscriptions or external database credentials are required for the discovery and feed-reading flow.

## Coverage limits

There is no central, complete list of all employers using each ATS. The URL index can miss new boards, custom-domain-only career pages, boards not yet archived, and sites that block crawlers. This is continuously expanded discovery, not a claim of perfect coverage. Common Crawl rate limits its index API; the crawler makes requests serially with delays and retains the previous board index if some provider-pattern searches fail.
