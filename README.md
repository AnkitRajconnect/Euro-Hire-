# GullyHire — automated live jobs and no-key company lookup

GullyHire is a static website connected to an automated Python crawler. The crawler runs in GitHub Actions, reads public employer career-board feeds, filters postings and writes the live results into `site/data/jobs.json`. GitHub Pages then publishes that website.

## Launch without editing code

1. Extract the ZIP.
2. Create a new **public** GitHub repository with the default branch `main`.
3. In **Settings → Pages → Build and deployment → Source**, choose **GitHub Actions**. This is the one required repository setting.
4. Upload every extracted file and folder into the repository root, including the hidden `.github` folder, and commit to `main`. On Mac, use **Command + Shift + .** in Finder to reveal hidden files.
5. Open **Actions** and wait for `GullyHire live crawler and website` to finish. Open the `github-pages` environment URL from the deployment job.

There are no API keys, external database credentials, secrets, or manual code edits required. GitHub's built-in token handles the workflow's repository updates. GitHub requires the repository owner to select a Pages publishing source; a no-key upload cannot perform that privileged setting itself.

## Live job sources

`companies.yaml` contains seven curated employer career-board connections:

| Employer | Public job-board feed |
|---|---|
| Spotify | Lever |
| ElevenLabs | Ashby |
| Synthesia | Ashby |
| Supabase | Ashby |
| Linear | Ashby |
| Hugging Face | Workable |
| Stripe | Greenhouse |

The crawler uses public posting feeds; the launch configuration does not need an API key. The feeds are called directly by the Python crawler on GitHub's hosted runner, not by the browser. If an employer changes its ATS vendor or public board slug, that one source can fail until its configuration is updated. Partial feed failures do not stop good employers' results from being processed. If all feeds fail, the crawler exits unsuccessfully and does not replace the previous `jobs.json` file.

## France-first employer discovery

France discovery is based on **active registered legal entities with evidence of having employed staff**, not every SIRENE record. The SIRENE employee band is retained with its reference year:

- `00` and `01`–`53`: employer candidates (the `00` band means staff were employed during the reference year, even if none remained at 31 December).
- `NN`: non-employer; excluded from the France discovery queue.
- Blank or unrecognised band: unresolved; retained in the coverage report but not crawled.

INSEE warns that employee bands are statistical and may be two or three years old. They are not a live headcount. Some records have no employee band; Euro-Hire must show these as unresolved rather than silently treating them as employers. Partially diffused personal records are excluded from this public-data import.

### Load the official French register

Download the official INSEE SIRENE **StockUniteLegale** full stock file and inspect its real column names before mapping:

```bash
python -m crawler.company_registry init
python -m crawler.company_registry inspect-registry-file --file /path/to/StockUniteLegale.csv
```

Create a JSON mapping using the exact headers returned by the inspection command. The core mapping fields are:
- `number_column`: SIREN identifier
- `name_column`: legal name
- `status_column`: administrative status (`A` active, `C` ceased)
- `active_values`: [`A`]
- `inactive_values`: [`C`]
- `legal_form_column`: legal-form code
- `legal_form_prefixes`: reviewed legal-person prefixes
- `employee_range_column`: SIRENE employee band (normally `trancheEffectifsUniteLegale`)
- `employee_range_date_column`: employee-band reference year (normally `anneeEffectifsUniteLegale`)
- `diffusion_column`: diffusion status (normally `statutDiffusionUniteLegale`)
- `excluded_diffusion_values`: [`P`, `N`]
- `snapshot_date`: date of the downloaded official snapshot
- `full_snapshot`: `true` only when the downloaded file is the complete official snapshot

Import only after checking the mapping against the file:

```bash
python -m crawler.company_registry import-fr-stock --file /path/to/StockUniteLegale.csv --mapping /path/to/fr-mapping.json --confirm-full-snapshot
python -m crawler.company_registry coverage-fr
python -m crawler.company_registry discover-all --country FR --limit 100
python -m crawler.company_registry coverage-fr
```

The discovery batch only visits confirmed employer candidates with fresh registry data and a verified official domain. A company with no linked careers page, a blocked website, a stale registry record or an unknown employee band remains visible in the coverage report. Only sources supported by the employer's own website can enter the approved job crawler. The local SQLite registry is an operational database; it is not currently synced automatically to GitHub Pages.

## Company database

The Company database page searches the public GLEIF Global LEI Index directly. It can search entities, save a local shortlist and export CSV without an API key or account. GLEIF contains entities that have a Legal Entity Identifier (LEI); it is **not** a complete database of every small/local business.

## Important verification distinction

The curated employer list verifies the relationship between the configured employer domain/careers page and its public ATS board. It is **not** a claim that each employer has been independently checked against every national company register. The job feed itself is read live, and vacancy URLs must match the approved source. Jobs that do not pass the role and Europe filters are not published. Applicants should always confirm that the vacancy remains open on the employer's own application page.

## Real data only

The shipped `site/data/jobs.json` is deliberately empty. No sample, fictional, generated or placeholder job rows are included. After a successful run, only listings returned by the configured public employer feeds and accepted by the crawler are published. If no roles pass the filters, the website shows an empty-state message.

## Refresh and monitoring

- Automatic refresh: daily at 05:15 UTC.
- Also runs on every push to `main` and can be started manually from **Actions → GullyHire live crawler and website → Run workflow**.
- `site/data/report.json` contains per-feed status and publication counts.
- The `gullyhire-review-queue` artifact holds suspicious or unresolved jobs for seven days; these are not published.
- If every public feed is unavailable, the current live dataset is preserved and the workflow reports failure.

## Optional local checks

```bash
python -m pip install -r requirements.txt
python -m pytest -q
python -m crawler.crawl --curated-config --verify
```

The final command calls the live employer feeds and therefore needs internet access. GitHub Actions runs it in an online GitHub-hosted environment.
