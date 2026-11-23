"""Find out which ATS a company uses, to grow companies.yaml.

    python -m crawler.discover https://example.com/careers [more urls...]

Prints ready-to-paste YAML lines for every supported ATS it recognises on the page.
"""
from __future__ import annotations

import re
import sys

from .http import FetchError, Http

PATTERNS = [
    ("greenhouse", re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"jobs\.lever\.co/([a-z0-9_-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([a-z0-9_.%-]+)", re.I)),
    ("personio", re.compile(r"([a-z0-9-]+)\.jobs\.personio\.(?:de|com)", re.I)),
    ("recruitee", re.compile(r"([a-z0-9-]+)\.recruitee\.com", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/([a-z0-9_-]+)", re.I)),
    ("smartrecruiters", re.compile(r"(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9_-]+)", re.I)),
    ("teamtailor", re.compile(r"([a-z0-9-]+)\.teamtailor\.com", re.I)),
]
IGNORE = {"embed", "www", "api", "careers", "jobs", "app", "static", "cdn"}


def detect(page_html: str) -> list[tuple[str, str]]:
    found = []
    for ats, rx in PATTERNS:
        for m in rx.finditer(page_html):
            slug = m.group(1)
            if slug.lower() in IGNORE:
                continue
            if (ats, slug) not in found:
                found.append((ats, slug))
    # Workday career-site links contain a tenant, data-centre host, site and
    # optional locale. Preserve the case-sensitive site identifier.
    workday_rx = re.compile(
        r"(?:https?:)?//([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/"
        r"(?:(en-[A-Za-z]{2}|[a-z]{2}-[A-Z]{2})/)?([A-Za-z0-9_-]+)", re.I)
    for m in workday_rx.finditer(page_html):
        tenant, region, locale, site = m.groups()
        slug = "|".join((tenant.lower(), region.lower(), site, locale or "en-US"))
        if ("workday", slug) not in found:
            found.append(("workday", slug))
    return found


def main(urls: list[str]) -> int:
    http = Http()
    for url in urls:
        try:
            if not http.allowed_by_robots(url):
                print(f"# {url}: robots.txt disallows, skipped")
                continue
            hits = detect(http.get_text(url))
        except FetchError as e:
            print(f"# {url}: {e}")
            continue
        if not hits:
            print(f"# {url}: no supported ATS found (custom page; try the 'jsonld' type)")
        for ats, slug in hits:
            print(f"  - {{name: {slug}, slug: {slug}, ats: {ats}}}   # from {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]) if len(sys.argv) > 1 else print(__doc__))
