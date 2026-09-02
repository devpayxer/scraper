"""Find a client configuration eBay will answer.

This is a one-off diagnostic, not part of collection. It tries a short list of
browser profiles against a single eBay URL and reports what came back, so you
can put a working value in settings.yml instead of guessing.

It is deliberately a handful of spaced requests, run by hand. Nothing here
rotates profiles during normal collection -- the point is to find one setting
that works and then stay on it.
"""
from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass

from .config import Settings
from .fetch import _is_blocked, _looks_like_results
from .parse import parse_search_page
from .urls import sold_search_url

# Current-generation profiles, newest first. An old profile is worse than a new
# one: hardly any real Chrome 124 is still browsing in 2026.
DEFAULT_PROFILES = [
    "chrome150", "chrome146", "chrome142", "firefox147", "safari260", "chrome136",
]


# Statuses that mean eBay answered and said no, as opposed to never answering.
REFUSAL_STATUSES = {"403", "challenge", "no results markup", "empty"}


def diagnose(results: list["ProbeResult"]) -> str:
    """Summarise a probe run: worked / refused / unreachable.

    A refusal anywhere outranks network noise -- if eBay answered even once to
    say no, the connection is fine and the problem is that we are unwelcome.
    """
    if any(r.works for r in results):
        return "ok"
    if any(r.status in REFUSAL_STATUSES for r in results):
        return "refused"
    return "unreachable"


@dataclass
class ProbeResult:
    profile: str
    warm_up: bool
    status: str
    http: int | None = None
    listings: int = 0
    title: str = ""
    detail: str = ""

    @property
    def works(self) -> bool:
        return self.status == "ok"


def available_profiles() -> list[str]:
    try:
        import typing

        from curl_cffi.requests.impersonate import BrowserTypeLiteral

        return list(typing.get_args(BrowserTypeLiteral))
    except Exception:
        return []


def _page_title(html: str) -> str:
    match = re.search(r"<title>(.*?)</title>", html, re.S)
    return match.group(1).strip()[:60] if match else ""


def probe_profile(settings: Settings, profile: str, *, warm_up: bool,
                  url: str, timeout: int = 35) -> ProbeResult:
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        return ProbeResult(profile, warm_up, "error", detail="curl_cffi not installed")

    result = ProbeResult(profile, warm_up, "error")
    session = None
    try:
        session = cffi_requests.Session(impersonate=profile)
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        if warm_up:
            session.get(f"https://{settings.marketplace}/", headers=headers,
                        timeout=timeout)
            time.sleep(random.uniform(1.5, 3.0))

        response = session.get(url, headers=headers, timeout=timeout)
        html = response.text
        result.http = response.status_code
        result.title = _page_title(html)

        if response.status_code == 403:
            result.status = "403"
        elif _is_blocked(html):
            result.status = "challenge"
        elif not _looks_like_results(html):
            result.status = "no results markup"
        else:
            result.listings = len(parse_search_page(html))
            result.status = "ok" if result.listings else "empty"
    except Exception as exc:
        result.status = "network"
        result.detail = f"{type(exc).__name__}: {exc}"[:70]
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
    return result


def run_probe(settings: Settings, *, profiles: list[str] | None = None,
              seller: str | None = None, delay: float = 6.0,
              try_warm_up: bool = True) -> list[ProbeResult]:
    """Try each profile once (optionally twice: cold and warmed)."""
    from .config import Seller

    target = Seller(user=seller) if seller else None
    url = sold_search_url(settings, target, page=1)

    results: list[ProbeResult] = []
    for profile in (profiles or DEFAULT_PROFILES):
        for warm in ([False, True] if try_warm_up else [False]):
            results.append(probe_profile(settings, profile, warm_up=warm, url=url))
            if results[-1].works:
                return results          # stop at the first thing that works
            time.sleep(delay + random.uniform(0, 3))
    return results
