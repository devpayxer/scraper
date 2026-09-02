"""HTTP layer: polite, cached, retrying page fetches.

Two engines:
  curl_cffi  -- impersonates a real Chrome TLS/JA3 fingerprint. This is what
                makes eBay answer at all; plain requests/urllib get a 403.
  playwright -- a real headless Chromium. Slower, but survives layouts that
                only render client-side.

Everything is cached to disk, so re-running a scrape is free and you can
re-parse yesterday's HTML after improving the parser (`reparse`).
"""
from __future__ import annotations

import hashlib
import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import Pacing, Settings

log = logging.getLogger(__name__)

# Signatures of a challenge/refusal page rather than results. Several of these
# come from a real eBay interstitial captured while building this -- see
# tests/fixtures/blocked_challenge.html.
BLOCK_MARKERS = (
    "Pardon Our Interruption",
    "Checking your browser",
    "captcha",
    "unusual traffic",
    "Error Page | eBay",
    "Security Measure",          # eBay's current challenge page title
    "verify yourself to continue",
    "splashui",
)


class BlockedError(RuntimeError):
    """eBay served a challenge/blocked page instead of results."""


class FetchError(RuntimeError):
    pass


@dataclass
class Response:
    url: str
    html: str
    from_cache: bool = False


def _cache_path(cache_dir: Path, url: str) -> Path:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return cache_dir / digest[:2] / f"{digest}.html"


def _is_blocked(html: str) -> bool:
    head = html[:6000]
    return any(marker.lower() in head.lower() for marker in BLOCK_MARKERS)


def _looks_like_results(html: str) -> bool:
    """Cheap sanity check that we got a search results page at all."""
    needles = ("srp-results", "s-item", "s-card", "srp-river", "brwrvr__item")
    return any(n in html for n in needles)


class Fetcher:
    def __init__(
        self,
        settings: Settings,
        *,
        use_cache: bool = True,
        engine: str | None = None,
        pacing: Pacing | None = None,
        on_fetch: Callable[[str, str], None] | None = None,
    ):
        self.settings = settings
        self.use_cache = use_cache
        self.engine = engine or settings.engine
        self.pacing = pacing or settings.pacing()
        self.on_fetch = on_fetch
        self.cache_dir = settings.resolve("cache_dir")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._last_request = 0.0
        self._session = None
        self._browser = None
        self._playwright = None
        self.pages_fetched = 0

    # ------------------------------------------------------------- caching
    def _read_cache(self, url: str) -> str | None:
        if not self.use_cache:
            return None
        path = _cache_path(self.cache_dir, url)
        if not path.exists():
            return None
        age_hours = (time.time() - path.stat().st_mtime) / 3600
        if self.settings.cache_ttl_hours and age_hours > self.settings.cache_ttl_hours:
            return None
        return path.read_text(encoding="utf-8", errors="replace")

    def _write_cache(self, url: str, html: str) -> None:
        path = _cache_path(self.cache_dir, url)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")

    def cached_pages(self) -> list[Path]:
        return sorted(self.cache_dir.rglob("*.html"))

    # ------------------------------------------------------------ throttle
    def _sleep_between_requests(self) -> None:
        """Wait a human-ish, uneven amount of time before the next page.

        Two gaps, not one: an ordinary between-pages pause, plus a longer break
        every `long_pause_every` pages. Steady clockwork requests are the most
        machine-looking thing a client can do, and they are also simply more
        load than this needs to place on eBay.
        """
        pacing = self.pacing
        if pacing.long_pause_every and self.pages_fetched and \
                self.pages_fetched % pacing.long_pause_every == 0:
            pause = pacing.long_pause_seconds * random.uniform(0.7, 1.3)
            log.info("pausing %.0fs after %s pages", pause, self.pages_fetched)
            time.sleep(pause)
            self._last_request = time.time()
            return

        wait = pacing.delay_seconds + random.uniform(0, pacing.delay_jitter)
        elapsed = time.time() - self._last_request
        if self._last_request and elapsed < wait:
            time.sleep(wait - elapsed)

    # ------------------------------------------------------------- engines
    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
                      "image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Referer": f"https://{self.settings.marketplace}/",
            "Upgrade-Insecure-Requests": "1",
        }

    def _fetch_curl_cffi(self, url: str) -> str:
        if self._session is None:
            try:
                from curl_cffi import requests as cffi_requests
            except ImportError as exc:  # pragma: no cover
                raise FetchError(
                    "curl_cffi is not installed. `pip install curl_cffi`, or run with "
                    "--engine playwright."
                ) from exc
            try:
                self._session = cffi_requests.Session(
                    impersonate=self.settings.impersonate)
            except Exception as exc:
                raise FetchError(
                    f"curl_cffi rejected impersonate={self.settings.impersonate!r}. "
                    f"Run `python -m ebayparts probe --list` to see valid targets."
                ) from exc
            self._warm_up()
        response = self._session.get(
            url, headers=self._headers(), timeout=self.settings.timeout_seconds,
            allow_redirects=True,
        )
        if response.status_code in (403, 429):
            raise BlockedError(f"HTTP {response.status_code} from eBay")
        if response.status_code >= 400:
            raise FetchError(f"HTTP {response.status_code} for {url}")
        return response.text

    def _warm_up(self) -> None:
        """Land on the homepage before searching.

        A person arrives at ebay.com and then searches; they do not appear
        directly at a deep sold-search URL with no cookies. This is one request
        per run, and it is also simply the correct way to acquire a session.
        """
        if not getattr(self.settings, "warm_up", False) or self._session is None:
            return
        home = f"https://{self.settings.marketplace}/"
        try:
            self._session.get(home, headers=self._headers(),
                              timeout=self.settings.timeout_seconds)
            log.debug("warm-up visit to %s ok", home)
            time.sleep(random.uniform(1.5, 4.0))
        except Exception as exc:
            log.debug("warm-up visit failed (continuing): %s", exc)

    def _ensure_browser(self):
        """Launch a persistent Chromium profile.

        Persistent, not a fresh context per page: cookies and local storage
        survive between runs, so the second visit looks like a returning
        visitor rather than a brand-new browser every single time.

        Deliberately NOT patched for stealth -- no
        --disable-blink-features=AutomationControlled, no webdriver masking, no
        stealth plugin. This is an honest Playwright Chromium. If eBay declines
        to serve automation, that is an answer, not an obstacle to route around.
        """
        if self._browser is not None:
            return self._browser
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover
            raise FetchError(
                "playwright is not installed. Run:\n"
                "    pip install playwright\n"
                "    playwright install chromium"
            ) from exc

        profile_dir = self.settings.resolve("browser_profile_dir")
        profile_dir.mkdir(parents=True, exist_ok=True)
        options: dict[str, object] = {
            "user_data_dir": str(profile_dir),
            "headless": self.settings.browser_headless,
            "viewport": {"width": 1440, "height": 900},
            "locale": "en-US",
            "slow_mo": self.settings.browser_slow_mo_ms or 0,
        }
        if self.settings.browser_executable:
            options["executable_path"] = self.settings.browser_executable

        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch_persistent_context(**options)
        except Exception as exc:
            # A half-started Playwright poisons every later attempt with a
            # misleading "Sync API inside the asyncio loop" error, hiding the
            # real cause. Tear it down so the next try reports the truth.
            self._shutdown_browser()
            hint = ""
            if "Executable doesn't exist" in str(exc):
                hint = ("\n    The browser is not installed. Run:  playwright install chromium"
                        "\n    Or point browser_executable in settings.yml at an existing Chrome.")
            raise FetchError(f"could not start the browser: {exc}{hint}") from exc

        self._browser.set_default_timeout(self.settings.timeout_seconds * 1000)
        log.info("browser profile: %s (%s)", profile_dir,
                 "headless" if self.settings.browser_headless else "visible")
        return self._browser

    def _fetch_playwright(self, url: str) -> str:
        context = self._ensure_browser()
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(url, wait_until="domcontentloaded",
                  timeout=self.settings.timeout_seconds * 1000)
        # eBay renders results server-side; this is just settling time, not a
        # scripted imitation of a person reading the page.
        page.wait_for_timeout(random.randint(1200, 2600))
        return page.content()

    # --------------------------------------------------------------- fetch
    def get(self, url: str) -> Response:
        cached = self._read_cache(url)
        if cached is not None:
            log.debug("cache hit %s", url)
            if self.on_fetch:
                self.on_fetch(url, "cached")
            return Response(url=url, html=cached, from_cache=True)

        backoff = self.settings.retry_backoff
        last_error: Exception | None = None

        for attempt in range(1, self.settings.max_retries + 1):
            self._sleep_between_requests()
            self._last_request = time.time()
            try:
                html = (
                    self._fetch_playwright(url)
                    if self.engine == "playwright"
                    else self._fetch_curl_cffi(url)
                )
                if _is_blocked(html):
                    raise BlockedError("challenge/interstitial page returned")
                self._write_cache(url, html)
                self.pages_fetched += 1
                if self.on_fetch:
                    self.on_fetch(url, "ok")
                return Response(url=url, html=html)
            except BlockedError as exc:
                last_error = exc
                if self.on_fetch:
                    self.on_fetch(url, "blocked")
                # Never retry into a refusal. If eBay said no, the correct
                # response is to stop, not to knock harder.
                log.warning("blocked: %s -- not retrying", exc)
                raise
            except Exception as exc:  # network hiccup, timeout, ...
                last_error = exc
                if self.on_fetch:
                    self.on_fetch(url, "error")
                log.warning("fetch error attempt %s/%s: %s", attempt,
                            self.settings.max_retries, exc)
                if attempt < self.settings.max_retries:
                    time.sleep(backoff)
                    backoff *= 2

        raise FetchError(f"giving up on {url}: {last_error}")

    def open_browser(self, url: str | None = None) -> None:
        """Open the persistent profile for a one-off manual visit."""
        context = self._ensure_browser()
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(url or f"https://{self.settings.marketplace}/",
                  wait_until="domcontentloaded")

    def _shutdown_browser(self) -> None:
        for attr, stop in (("_browser", "close"), ("_playwright", "stop")):
            handle = getattr(self, attr, None)
            if handle is not None:
                try:
                    getattr(handle, stop)()
                except Exception:
                    pass
                setattr(self, attr, None)

    def close(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None
        self._shutdown_browser()

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
