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
