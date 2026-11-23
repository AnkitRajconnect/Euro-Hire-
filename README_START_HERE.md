# Start here — GullyHire OneClick

**No code edits, API keys, database setup or Python installation are required.** There is one mandatory GitHub Pages setting (below).

## One-time launch

1. Download and extract this ZIP on your computer.
2. Create a new **public** repository on GitHub. Keep the default branch as `main`.
3. Before uploading, open the new repository's **Settings → Pages → Build and deployment → Source** and select **GitHub Actions**. GitHub requires the owner to enable this publishing source; a no-key workflow cannot change repository settings on your behalf.
4. Upload **all files and folders** from the extracted `GullyHire_OneClick` folder into the repository root, then commit to `main`. The hidden `.github` folder is essential because it contains the crawler workflow. On a Mac, press **Command + Shift + .** in Finder to show hidden files before selecting everything.
5. Open **Actions**. The `GullyHire live crawler and website` workflow will run automatically. When the deployment job is green, click its **github-pages** environment URL to open GullyHire.

No environment variables, API secrets or personal access tokens need to be added. GitHub's built-in `GITHUB_TOKEN` is used for the crawler's own commits and deployment permissions.

## What runs automatically

- The Python crawler runs on a GitHub-hosted runner immediately after the first push and every day at 05:15 UTC.
- It checks seven configured employer boards in `companies.yaml` through their public Greenhouse, Lever, Ashby or Workable feed.
- It keeps only listings that pass the configured source, URL, occupation and Europe checks.
- It updates `site/data/jobs.json`, writes a crawl report, uploads held items as a short-lived workflow artifact, and deploys the updated static site.
- If all external feeds fail, the run fails safely and leaves the previously published job data unchanged.

## First visit

The shipped `site/data/jobs.json` contains **zero rows**. That is intentional: there are no sample jobs, fake employers or placeholder job cards. Real employer-feed listings appear after a successful scheduled/manual crawl. If no current role passes the Europe and role filters, the genuine result is an empty-state message rather than invented content.

The **Company database** page uses the free public GLEIF Global LEI Index from the browser. It needs no account or API key. GLEIF covers legal entities with a Legal Entity Identifier, not every company in the world.

For the source/registration distinction and troubleshooting, see `README.md`.
