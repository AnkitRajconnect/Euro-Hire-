"""Polite HTTP client: identifies itself, rate-limits per host, retries on 429/5xx."""
from __future__ import annotations

import os
import threading
import time
from urllib.parse import urlparse
from urllib import robotparser

import requests

CONTACT = os.environ.get("GULLYHIRE_CONTACT") or os.environ.get("EUROHIRE_CONTACT", "set-GULLYHIRE_CONTACT-env-var")
USER_AGENT = f"GullyHireBot/0.2 (+job discovery project, links back to employers; contact: {CONTACT})"


class FetchError(Exception):
    pass


class Http:
    def __init__(self, min_delay: float = 0.5, timeout: float = 20.0, retries: int = 3):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.min_delay = min_delay
        self.timeout = timeout
        self.retries = retries
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}

    def _wait(self, host: str) -> None:
        with self._lock:
            now = time.monotonic()
            nxt = max(now, self._last.get(host, 0.0) + self.min_delay)
            self._last[host] = nxt
        if nxt > now:
            time.sleep(nxt - now)

    def get(self, url: str, **kw) -> requests.Response:
        host = urlparse(url).netloc
        last_err: Exception | None = None
        for attempt in range(self.retries):
            self._wait(host)
            try:
                r = self.session.get(url, timeout=self.timeout, **kw)
            except requests.RequestException as e:
                last_err = e
            else:
                if r.status_code == 404:
                    raise FetchError(f"404 not found: {url}")
                if r.status_code in (429, 500, 502, 503, 504):
                    last_err = FetchError(f"{r.status_code} from {url}")
                    time.sleep(2 ** attempt * 2)
                    continue
                if r.status_code >= 400:
                    raise FetchError(f"{r.status_code} from {url}")
                return r
            time.sleep(2 ** attempt)
        raise FetchError(f"giving up on {url}: {last_err}")

    def get_json(self, url: str, **kw):
        r = self.get(url, **kw)
        try:
            return r.json()
        except ValueError as e:
            raise FetchError(f"invalid JSON from {url}: {e}") from e

    def get_text(self, url: str, **kw) -> str:
        return self.get(url, **kw).text


    def post_json(self, url: str, payload: dict, **kw):
        """POST JSON only to a public Workday careers host.

        This deliberately narrow method is used for the unauthenticated job
        search endpoint called by public Workday career sites. It does not
        support arbitrary POST destinations or follow redirects.
        """
        p = urlparse(url)
        host = (p.hostname or "").lower()
        if (p.scheme != "https" or not host.endswith(".myworkdayjobs.com")
                or not p.port in (None, 443) or p.username or p.password):
            raise FetchError("POST is restricted to HTTPS public Workday career hosts")
        last_err: Exception | None = None
        for attempt in range(self.retries):
            self._wait(host)
            try:
                response = self.session.post(url, json=payload, timeout=self.timeout,
                                             allow_redirects=False, **kw)
            except requests.RequestException as exc:
                last_err = exc
            else:
                if response.status_code in (301, 302, 303, 307, 308):
                    raise FetchError(f"Workday careers endpoint redirected; refusing to follow POST redirect: {url}")
                if response.status_code in (429, 500, 502, 503, 504):
                    last_err = FetchError(f"{response.status_code} from {url}")
                    time.sleep(2 ** attempt * 2)
                    continue
                if response.status_code >= 400:
                    raise FetchError(f"{response.status_code} from {url}")
                try:
                    return response.json()
                except ValueError as exc:
                    raise FetchError(f"invalid JSON from {url}: {exc}") from exc
            time.sleep(2 ** attempt)
        raise FetchError(f"giving up on {url}: {last_err}")

    def allowed_by_robots(self, url: str) -> bool:
        """Check robots.txt. Used for anything that is NOT an official ATS feed."""
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp = robotparser.RobotFileParser()
            try:
                r = self.session.get(base + "/robots.txt", timeout=self.timeout)
                if r.status_code in (404, 410):
                    rp = None  # no robots policy published -> allowed
                elif r.status_code >= 400:
                    rp.parse(["User-agent: *", "Disallow: /"])
                else:
                    rp.parse(r.text.splitlines())
            except requests.RequestException:
                # When we cannot read the policy, do not assume permission to crawl.
                rp.parse(["User-agent: *", "Disallow: /"])
            self._robots[base] = rp
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch(USER_AGENT, url)
