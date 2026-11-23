# QA report — GullyHire OneClick

Checks performed in the build environment:

- Automated Python tests: 50 passing, including curated source approval, board/hostname mismatch rejection, real-only initial data, Ashby role normalization and fail-safe handling of total feed outages.
- Curated employer sources: 7/7 configurations pass the local source/publication gate.
- Starting public dataset: `site/data/jobs.json` contains zero listings; the 57 fictional Northwind Labs example jobs and demo generator have been removed.
- Browser guard: the site rejects demo-marked data, demo IDs and non-HTTPS job URLs.
- GitHub workflow: starts on a push to `main`, daily schedule and manual dispatch; runs tests before live crawling; commits updated data with the built-in token; uploads the review queue; then deploys the site through GitHub Pages.
- Outage protection: if no employer feed responds, the crawler exits with an error and leaves the last known `jobs.json` unchanged.
- Review queue: held vacancies are not published; the workflow artifact expires after seven days.
- Company directory: the page uses the public GLEIF API directly and stores the user's shortlist in local browser storage.

Live external API requests could not be run from the build container because outbound DNS/network access is blocked. Some public endpoint responses were independently opened during preparation (Spotify Lever; Ashby for ElevenLabs, Supabase and Linear). The first GitHub Actions run is the live integration check for every configured employer board. ATS vendors and employer board slugs can change; one failing source is reported without blocking the other successful sources. If all feeds fail, the first deploy is stopped rather than publishing fake or empty-success data.

GitHub Pages requires the repository owner to select **Settings → Pages → Source → GitHub Actions** once. This is a GitHub repository setting, not a code edit or API credential.
