# Euro-Hire

Euro-Hire is a Europe-focused job discovery site. It reads public job feeds from employer career boards, keeps Europe-eligible roles, and shows the openings on a searchable list and map.

## How it works

1. `companies.yaml` lists employers whose public career boards have been checked.
2. The crawler reads each board's public ATS feed directly. No paid job API or national company-register database is required.
3. Each posting is checked for an HTTPS vacancy link and a European location.
4. Live openings are written to `site/data/jobs.json`; feed status is written to `site/data/report.json`.
5. GitHub Pages publishes the static website. The list and map use the same jobs file.

The workflow runs daily at 05:15 UTC, on pushes to `main`, and when started manually from GitHub Actions.

## Current employer sources

| Employer | Public board |
|---|---|
| Spotify | Lever |
| ElevenLabs | Ashby |
| Synthesia | Ashby |
| Supabase | Ashby |
| Linear | Ashby |
| Hugging Face | Workable |
| Stripe | Greenhouse |

The crawler also contains adapters for Personio, Recruitee, SmartRecruiters, Teamtailor and Workday. Those adapters are not automatically activated: add an employer only after checking the ATS link on its own careers page and testing the public board.

## Add an employer

1. Open the employer's own careers page.
2. Confirm the ATS board it links to and note the exact board slug.
3. Add its name, slug, ATS, official domain, official careers URL, board URL, headquarters country and review date to `companies.yaml`.
4. Run:

```bash
python -m pip install -r requirements.txt
python -m pytest -q
python -m crawler.crawl --companies companies.yaml --verify
```

Never guess board slugs or treat third-party company directories as first-party proof.

## Run locally

```bash
python -m pip install -r requirements.txt
python -m crawler.crawl --companies companies.yaml --out site/data
python -m pytest -q
node --check site/app.js
```

No API key or external database is needed.

## Crawl boundaries

- The live crawler reads public employer ATS feeds, not private applicant systems.
- Vacancy URLs must match the configured public board.
- Only jobs with a European location are included. Generic remote roles are included only for Europe-headquartered employers; explicit "Remote Europe" roles receive a Europe-wide map marker.
- If all feeds fail, the last successful job file is preserved.
- No sample or fictional jobs are published. Held vacancies are kept in a short-lived workflow artifact.
