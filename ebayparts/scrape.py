"""Scrape orchestration: walk sellers, walk pages, store rows."""
from __future__ import annotations

import datetime as dt
import logging
from collections import Counter
from dataclasses import dataclass

from .config import Pacing, Seller, Settings
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


class BudgetExhausted(RuntimeError):
    """The daily page budget is spent. Not an error -- the point of the design."""


class DayBudget:
    """Counts pages against a per-calendar-day allowance, across runs.

    Spanning runs matters: Task Scheduler firing twice, or a manual run after
    a scheduled one, must not double the day's traffic.
    """

    def __init__(self, store: Store, limit: int, today: dt.date | None = None):
        self.store = store
        self.limit = limit
        self.today = today or dt.date.today()
        self.already_used = store.pages_fetched_today(self.today)
        self.used_this_run = 0

    @property
    def used(self) -> int:
        return self.already_used + self.used_this_run

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def spend(self, pages: int = 1) -> None:
        self.used_this_run += pages
        if self.remaining <= 0:
            raise BudgetExhausted(
                f"daily budget spent ({self.used}/{self.limit} pages)"
            )

    def check(self) -> None:
        if self.remaining <= 0:
            raise BudgetExhausted(
                f"daily budget spent ({self.used}/{self.limit} pages)"
            )


def scrape_seller(
    seller: Seller,
    settings: Settings,
    fetcher: Fetcher,
    store: Store,
    *,
    pacing: Pacing,
    budget: DayBudget,
    max_pages: int | None = None,
    since: dt.date | None = None,
) -> SellerResult:
    """Page through one seller, stopping as soon as there is nothing new.

    After the first backfill this almost always stops on page 1 or 2: a seller
    who sold 20 things since yesterday puts them all on the first page, and
    every page after that is sales we already hold.
    """
    started = dt.datetime.utcnow().isoformat(timespec="seconds")
    result = SellerResult(seller=seller.user)
    limit = max_pages or pacing.pages_per_seller
    seen_ids: set[str] = set()
    quiet_pages = 0

    for page in range(1, limit + 1):
        budget.check()
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

        if not response.from_cache:
            budget.spend()

        listings = parse_search_page(response.html, source_url=url)
        if page == 1:
            total = result_count(response.html)
            if total:
                log.info("%s: eBay reports ~%s sold results", seller.user, total)

        fresh = [item for item in listings if item.item_id not in seen_ids]
        seen_ids.update(item.item_id for item in fresh)
        if since:
            fresh = [i for i in fresh if i.sold_date is None or i.sold_date >= since]

        # eBay repeats the last page forever instead of 404ing.
        if not listings or (page > 1 and not fresh):
            log.info("%s: page %s exhausted the results", seller.user, page)
            result.pages = page
            break

        found, new = store.upsert_many(fresh)
        result.pages = page
        result.rows_found += found
        result.rows_new += new
        log.info("%s page %-3s %3s rows (%s new)%s", seller.user, page, found, new,
                 " [cached]" if response.from_cache else "")

        # The incremental stop. Nothing new on this page means we have caught
        # up with this seller, and every further page is pure waste.
        quiet_pages = quiet_pages + 1 if new == 0 else 0
        if quiet_pages >= pacing.pages_without_new:
            log.info("%s: caught up (%s page(s) with nothing new)", seller.user, quiet_pages)
            break

    store.mark_seller_attempt(
        seller.user, pages=result.pages, new_rows=result.rows_new,
        status=result.status,
        backfilled=True if (result.status == "ok" and result.pages < limit) else None,
    )
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
    mode: str | None = None,
    max_pages: int | None = None,
    since: dt.date | None = None,
    use_cache: bool = True,
    engine: str | None = None,
    ignore_hours: bool = False,
    now: dt.datetime | None = None,
) -> list[SellerResult]:
    """One passive run: a few sellers, a capped number of pages, then stop."""
    now = now or dt.datetime.now()
    pacing = settings.pacing(mode)
    results: list[SellerResult] = []

    if not ignore_hours and not pacing.within_active_hours(now):
        start, end = pacing.active_hours
        log.warning(
            "declining to run at %02d:%02d -- active hours are %02d:00-%02d:00. "
            "Nothing is lost; eBay serves the full 90 days whenever you next run.",
            now.hour, now.minute, start, end,
        )
        return results

    budget = DayBudget(store, pacing.daily_page_budget, now.date())
    if budget.remaining <= 0:
        log.warning("daily budget already spent (%s/%s pages). Stopping.",
                    budget.used, budget.limit)
        return results

    active = [s for s in sellers if s.enabled]
    order = store.sellers_by_staleness([s.user for s in active])
    by_user = {s.user: s for s in active}
    queue = [by_user[u] for u in order][: pacing.sellers_per_run]

    log.info(
        "%s run: %s of %s seller(s), budget %s of %s pages left today",
        (mode or settings.mode), len(queue), len(active), budget.remaining, budget.limit,
    )

    with Fetcher(settings, use_cache=use_cache, engine=engine, pacing=pacing,
                 on_fetch=lambda url, outcome: store.record_fetch(None, url, outcome)
                 ) as fetcher:
        for index, seller in enumerate(queue, 1):
            log.info("[%s/%s] %s", index, len(queue), seller.name)
            try:
                result = scrape_seller(
                    seller, settings, fetcher, store, pacing=pacing, budget=budget,
                    max_pages=max_pages, since=since,
                )
            except BudgetExhausted as exc:
                log.info("stopping: %s", exc)
                break
            results.append(result)

            if result.status == "blocked" and pacing.stop_on_first_block:
                log.warning(
                    "eBay refused a request. Stopping for today rather than "
                    "pushing -- try again tomorrow, or raise the delays."
                )
                break

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
    pacing = settings.pacing()
    with Fetcher(settings, use_cache=use_cache, engine=engine, pacing=pacing) as fetcher:
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
