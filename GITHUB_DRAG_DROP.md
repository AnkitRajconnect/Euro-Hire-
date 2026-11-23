# GitHub drag-and-drop launch

**One code upload; no API key or code edits.**

1. Extract the ZIP.
2. Create a public repository on GitHub and leave the default branch as `main`.
3. Set **Settings → Pages → Build and deployment → Source** to **GitHub Actions** before uploading. This is a mandatory GitHub repository setting.
4. Open the extracted `GullyHire_OneClick` folder. Select all its contents and upload them into the repository root, keeping the directory structure. The `.github/workflows/daily.yml` file must be present. On macOS, press **Command + Shift + .** in Finder to reveal `.github`.
5. Commit the upload to `main`.
6. Open the **Actions** tab. Wait for `GullyHire live crawler and website` to complete, then click the URL shown under the `github-pages` deployment environment.

The first workflow performs tests, live feed retrieval, filters the results, writes the published job JSON and deploys the site. It repeats daily and after pushes. No fake/example jobs are bundled. Until the first successful crawl, the website clearly shows that it is waiting for live vacancies.
