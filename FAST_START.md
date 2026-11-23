# GullyHire — fast start

1. Extract the ZIP.
2. Create a public GitHub repository (default branch `main`).
3. Select **Settings → Pages → Source → GitHub Actions**.
4. Drag the extracted project contents into the repository root and commit them to `main`. Include hidden `.github`; on Mac press **Command + Shift + .** in Finder to show hidden files.
5. Open **Actions** and let `GullyHire live crawler and website` run. Open the deployment URL when the `github-pages` job succeeds.

You do not edit code, install Python, add a database, or create API credentials. The Pages setting is required by GitHub and must be selected once by the repository owner.

The website starts with **zero job rows**. The daily GitHub Actions crawler fetches current listings from the seven public employer feeds in `companies.yaml`, filters them, writes the real results and deploys the updated site. If the feeds are down, it preserves the last successful feed file rather than replacing it with an empty one.

Company search uses GLEIF's free public API. GLEIF is an LEI index, not a complete company directory.
