"""Scrape orchestration: walk sellers, walk pages, store rows."""
from __future__ import annotations

import datetime as dt
import logging
from collections import Counter
from dataclasses import dataclass

from .config import Seller, Settings
from .fetch import BlockedError, FetchError, Fetcher
from .parse import parse_search_page, result_count
from .store import Store
from .urls import category_browse_url, sold_search_url

log = logging.getLogger(__name__)


@dataclass
class SellerResult:
    seller: str
    pages: int = 0
    rows_found: int = 0
    rows_new: int = 0
    status: str = "ok"
    note: str = ""


def scrape_seller(
    seller: Seller,
    settings: Settings,
    fetcher: Fetcher,
    store: Store,
    *,
    max_pages: int | None = None,
    since: dt.date | None = None,
) -> SellerResult:
    """Page through one seller's sold listings and store what we find."""
    started = dt.datetime.utcnow().isoformat(timespec="seconds")
    result = SellerResult(seller=seller.user)
    limit = max_pages or settings.max_pages_per_seller
    seen_ids: set[str] = set()

    for page in range(1, limit + 1):
        url = sold_search_url(settings, seller, page=page)
        try:
            response = fetcher.get(url)
        except BlockedError as exc:
            result.status, result.note = "blocked", str(exc)
            log.error("%s: blocked on page %s -- %s", seller.user, page, exc)
            break
        except FetchError as exc:
            result.status, result.note = "error", str(exc)
            log.error("%s: fetch failed on page %s -- %s", seller.user, page, exc)
            break

        listings = parse_search_page(response.html, source_url=url)
        if page == 1:
            total = result_count(response.html)
            if total:
                log.info("%s: eBay reports ~%s sold results", seller.user, total)

        fresh = [item for item in listings if item.item_id not in seen_ids]
        seen_ids.update(item.item_id for item in fresh)

        if since:
            fresh = [i for i in fresh if i.sold_date is None or i.sold_date >= since]

        # eBay repeats the last page forever instead of 404ing, so an all-duplicate
        # page means we have reached the end.
        if not listings or (page > 1 and not fresh):
            log.info("%s: page %s exhausted the results", seller.user, page)
            result.pages = page
            break

        found, new = store.upsert_many(fresh)
        result.pages = page
        result.rows_found += found
        result.rows_new += new
        log.info(
            "%s page %-3s %3s rows (%s new)%s",
            seller.user, page, found, new, " [cached]" if response.from_cache else "",
        )

        if settings.stop_on_empty_page and not listings:
            break

    store.record_run(
        started_at=started,
        finished_at=dt.datetime.utcnow().isoformat(timespec="seconds"),
        seller=seller.user,
        pages=result.pages,
        rows_found=result.rows_found,
        rows_new=result.rows_new,
        status=result.status,
        note=result.note,
    )
    return result


def scrape_all(
    sellers: list[Seller],
    settings: Settings,
    store: Store,
    *,
    max_pages: int | None = None,
    since: dt.date | None = None,
    use_cache: bool = True,
    engine: str | None = None,
) -> list[SellerResult]:
    results: list[SellerResult] = []
    active = [s for s in sellers if s.enabled]
    with Fetcher(settings, use_cache=use_cache, engine=engine) as fetcher:
        for index, seller in enumerate(active, 1):
            log.info("[%s/%s] %s", index, len(active), seller.name)
            results.append(
                scrape_seller(seller, settings, fetcher, store,
                              max_pages=max_pages, since=since)
            )
    return results


def discover_sellers(
    settings: Settings,
    *,
    category: int | None = None,
    pages: int = 10,
    use_cache: bool = True,
    engine: str | None = None,
) -> list[tuple[str, int]]:
    """Walk category-wide sold results and rank the sellers that appear most.

    This is how you build the 50+ seller panel without hand-hunting usernames:
    the recyclers moving the most volume are the ones that keep showing up.
    """
    category = category or settings.default_category
    counter: Counter[str] = Counter()
    with Fetcher(settings, use_cache=use_cache, engine=engine) as fetcher:
        for page in range(1, pages + 1):
            url = category_browse_url(settings, category, page=page)
            try:
                response = fetcher.get(url)
            except (BlockedError, FetchError) as exc:
                log.error("discover stopped at page %s: %s", page, exc)
                break
            listings = parse_search_page(response.html, source_url=url)
            if not listings:
                break
            for item in listings:
                if item.seller:
                    counter[item.seller] += 1
            log.info("discover page %s: %s listings, %s sellers so far",
                     page, len(listings), len(counter))
    return counter.most_common()
