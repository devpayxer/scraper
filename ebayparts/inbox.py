"""Import sold-listing pages you saved from your own browser.

When eBay refuses automated requests, this is the route that still works and
stays entirely above board: you browse eBay yourself, in your own browser,
signed in as you, and save the pages you are already looking at. This module
just reads those files off your disk.

Workflow:
    python -m ebayparts urls --open     # opens each seller's sold page in tabs
    ...Ctrl+S each one into the inbox folder...
    python -m ebayparts import          # parses everything you saved
"""
from __future__ import annotations

import datetime as dt
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .parse import parse_search_page, result_count
from .store import Store

log = logging.getLogger(__name__)

HTML_SUFFIXES = {".html", ".htm", ".mhtml"}


@dataclass
class ImportResult:
    path: Path
    listings: int = 0
    stored: int = 0
    new: int = 0
    sellers: list[str] = field(default_factory=list)
    status: str = "ok"
    note: str = ""


def find_pages(folder: Path) -> list[Path]:
    """Every saved page in the folder, skipping the archive subfolder."""
    if not folder.exists():
        return []
    return sorted(
        p for p in folder.rglob("*")
        if p.is_file() and p.suffix.lower() in HTML_SUFFIXES
        and "imported" not in p.parts
    )


def import_page(path: Path, store: Store) -> ImportResult:
    """Parse one saved page into the database."""
    result = ImportResult(path=path)
    try:
        html = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        result.status, result.note = "unreadable", str(exc)
        return result

    from .fetch import _is_blocked

    if _is_blocked(html):
        result.status = "challenge"
        result.note = "this is a captcha/interstitial page, not results"
        return result

    listings = parse_search_page(html, source_url=str(path))
    result.listings = len(listings)
    if not listings:
        result.status = "empty"
        total = result_count(html)
        result.note = ("no listings found -- is this a sold-search results page?"
                       if total is None else f"page reports {total} results but none parsed")
        return result

    result.sellers = sorted({item.seller for item in listings if item.seller})
    result.stored, result.new = store.upsert_many(listings)
    return result


def import_folder(folder: Path, store: Store, *, archive: bool = False) -> list[ImportResult]:
    results = []
    for path in find_pages(folder):
        result = import_page(path, store)
        results.append(result)
        log.info("%s: %s listing(s), %s new%s", path.name, result.listings,
                 result.new, f" [{result.status}]" if result.status != "ok" else "")
        if archive and result.status == "ok":
            destination = folder / "imported" / dt.date.today().isoformat()
            destination.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(path), str(destination / path.name))
            except OSError as exc:
                log.warning("could not archive %s: %s", path.name, exc)
    return results
