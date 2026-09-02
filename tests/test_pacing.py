"""Tests for the passive-collection guarantees: budget, rotation, early stop.

These are the properties that keep request volume low, so they are the ones
worth pinning down.
"""
from __future__ import annotations

import datetime as dt

import pytest

from ebayparts.config import Pacing, Seller, Settings
from ebayparts.fetch import BlockedError, Response
from ebayparts.parse import parse_search_page
from ebayparts.scrape import BudgetExhausted, DayBudget, scrape_all, scrape_seller
from ebayparts.store import Store

TODAY = dt.date(2026, 9, 2)
CARD = """
<li class="s-item"><div class="s-item__wrapper">
  <a class="s-item__link" href="https://www.ebay.com/itm/{item_id}">
    <h3 class="s-item__title">{title}</h3></a>
  <div class="s-item__caption"><span class="s-item__caption--signal">Sold  Sep 1, 2026</span></div>
  <div class="s-item__subtitle"><span class="SECONDARY_INFO">Pre-Owned</span></div>
  <span class="s-item__price">${price}</span>
  <span class="s-item__shipping">+$10.00 delivery</span>
  <span class="s-item__seller-info-text">testseller 99.8% positive (1K)</span>
</div></li>"""


def page_html(start_id: int, count: int = 5) -> str:
    cards = "".join(
        CARD.format(item_id=300000000000 + start_id + i,
                    title=f"2015-2018 FORD F-150 LEFT HEADLIGHT OEM #{start_id + i}",
                    price=100 + i)
        for i in range(count)
    )
    return f"<html><body><ul class='srp-results'>{cards}</ul></body></html>"


class FakeFetcher:
    """Serves a distinct page per _pgn, and records every URL asked for."""

    def __init__(self, *, pages: int = 10, block_at: int | None = None):
        self.requested: list[str] = []
        self.pages = pages
        self.block_at = block_at
        self.pages_fetched = 0

    def get(self, url: str) -> Response:
        self.requested.append(url)
        if self.block_at is not None and len(self.requested) >= self.block_at:
            raise BlockedError("simulated block")
        page = int(url.split("_pgn=")[1].split("&")[0])
        if page > self.pages:
            return Response(url=url, html="<html><body></body></html>")
        self.pages_fetched += 1
        return Response(url=url, html=page_html(start_id=page * 100))

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


@pytest.fixture
def settings():
    return Settings.load()


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "pacing.db") as s:
        yield s


class TestDayBudget:
    def test_counts_across_separate_runs(self, store):
        for _ in range(5):
            store.record_fetch("a", "http://x", "ok")
        budget = DayBudget(store, limit=10, today=dt.date.today())
        assert budget.used == 5
        assert budget.remaining == 5

    def test_cache_hits_do_not_count(self, store):
        store.record_fetch("a", "http://x", "ok")
        store.record_fetch("a", "http://y", "cached")
        assert DayBudget(store, limit=10).used == 1

    def test_spending_past_the_limit_raises(self, store):
        budget = DayBudget(store, limit=2)
        budget.spend()
        with pytest.raises(BudgetExhausted):
            budget.spend()

    def test_check_raises_when_exhausted(self, store):
        for _ in range(3):
            store.record_fetch("a", "http://x", "ok")
        with pytest.raises(BudgetExhausted):
            DayBudget(store, limit=3).check()


class TestActiveHours:
    def test_inside(self):
        pacing = Pacing(active_hours=(8, 23))
        assert pacing.within_active_hours(dt.datetime(2026, 9, 2, 10, 0))

    def test_outside(self):
        pacing = Pacing(active_hours=(8, 23))
        assert not pacing.within_active_hours(dt.datetime(2026, 9, 2, 3, 0))

    def test_window_crossing_midnight(self):
        pacing = Pacing(active_hours=(22, 6))
        assert pacing.within_active_hours(dt.datetime(2026, 9, 2, 23, 0))
        assert pacing.within_active_hours(dt.datetime(2026, 9, 2, 2, 0))
        assert not pacing.within_active_hours(dt.datetime(2026, 9, 2, 12, 0))

    def test_run_declines_outside_hours(self, settings, store, monkeypatch):
        fake = FakeFetcher()
        monkeypatch.setattr("ebayparts.scrape.Fetcher", lambda *a, **k: fake)
        results = scrape_all(
            [Seller(user="testseller")], settings, store,
            now=dt.datetime(2026, 9, 2, 4, 0),
        )
        assert results == []
        assert fake.requested == []   # nothing was asked of eBay at all


class TestRotation:
    def test_never_visited_comes_first(self, store):
        store.mark_seller_attempt("a", pages=1, new_rows=0, status="ok")
        assert store.sellers_by_staleness(["a", "b"])[0] == "b"

    def test_only_a_slice_of_the_panel_per_run(self, settings, store, monkeypatch):
        fake = FakeFetcher(pages=1)
        monkeypatch.setattr("ebayparts.scrape.Fetcher", lambda *a, **k: fake)
        panel = [Seller(user=f"seller{i}") for i in range(20)]
        results = scrape_all(panel, settings, store, mode="passive",
                             ignore_hours=True)
        assert len(results) == settings.pacing("passive").sellers_per_run

    def test_consecutive_runs_visit_different_sellers(self, settings, store, monkeypatch):
        monkeypatch.setattr("ebayparts.scrape.Fetcher",
                            lambda *a, **k: FakeFetcher(pages=1))
        panel = [Seller(user=f"seller{i}") for i in range(20)]
        first = {r.seller for r in scrape_all(panel, settings, store,
                                              ignore_hours=True)}
        second = {r.seller for r in scrape_all(panel, settings, store,
                                               ignore_hours=True)}
        assert not (first & second), "a second run revisited the same sellers"


class TestEarlyStop:
    def test_stops_once_nothing_is_new(self, settings, store):
        """The whole point: a caught-up seller costs 2 pages, not 40."""
        # pre-load everything the fake serves, so every page is already known
        for page in range(1, 11):
            store.upsert_many(parse_search_page(page_html(start_id=page * 100)))

        fake = FakeFetcher(pages=10)
        pacing = Pacing(pages_per_seller=10, pages_without_new=2)
        result = scrape_seller(
            Seller(user="testseller"), settings, fake, store,
            pacing=pacing, budget=DayBudget(store, limit=100),
        )
        assert result.rows_new == 0
        assert result.pages == 2, "should have stopped after 2 quiet pages"
        assert len(fake.requested) == 2

    def test_keeps_going_while_rows_are_new(self, settings, store):
        fake = FakeFetcher(pages=10)
        pacing = Pacing(pages_per_seller=4, pages_without_new=2)
        result = scrape_seller(
            Seller(user="testseller"), settings, fake, store,
            pacing=pacing, budget=DayBudget(store, limit=100),
        )
        assert result.pages == 4
        assert result.rows_new == 20


class TestStopConditions:
    def test_budget_caps_pages_fetched(self, settings, store):
        fake = FakeFetcher(pages=50)
        pacing = Pacing(pages_per_seller=50, pages_without_new=99)
        budget = DayBudget(store, limit=7)
        with pytest.raises(BudgetExhausted):
            scrape_seller(Seller(user="testseller"), settings, fake, store,
                          pacing=pacing, budget=budget, max_pages=50)
        assert len(fake.requested) == 7

    def test_a_block_ends_the_run_without_retrying(self, settings, store, monkeypatch):
        fake = FakeFetcher(pages=10, block_at=2)
        monkeypatch.setattr("ebayparts.scrape.Fetcher", lambda *a, **k: fake)
        panel = [Seller(user=f"seller{i}") for i in range(6)]
        results = scrape_all(panel, settings, store, ignore_hours=True)
        assert results[-1].status == "blocked"
        assert len(results) == 1, "the run should stop, not move to the next seller"
        assert len(fake.requested) == 2, "no retry into a refusal"


class TestStaleness:
    def test_bands(self):
        from ebayparts.store import staleness_status
        assert staleness_status(None) == "pending"
        assert staleness_status(0) == "ok"
        assert staleness_status(44) == "ok"
        assert staleness_status(45) == "stale"
        assert staleness_status(71) == "stale"
        assert staleness_status(72) == "at risk"
        assert staleness_status(89) == "at risk"
        assert staleness_status(90) == "gap"
        assert staleness_status(400) == "gap"

    def test_bands_scale_with_the_window(self):
        from ebayparts.store import staleness_status
        # a 30-day window: ok < 15, stale 15-24, at risk 24-30, gap >= 30
        assert staleness_status(14, lookback_days=30) == "ok"
        assert staleness_status(20, lookback_days=30) == "stale"
        assert staleness_status(29, lookback_days=30) == "at risk"
        assert staleness_status(30, lookback_days=30) == "gap"

    def test_report_uses_last_success_not_last_attempt(self, store):
        """A seller blocked every run is NOT fresh, however often it is tried."""
        store.mark_seller_attempt("blocked_one", pages=0, new_rows=0, status="blocked")
        report = {r["seller"]: r for r in store.coverage_report(["blocked_one"])}
        assert report["blocked_one"]["status"] == "pending"
        assert report["blocked_one"]["last_success"] is None

    def test_report_flags_a_drifted_seller(self, store):
        store.mark_seller_attempt("old", pages=1, new_rows=1, status="ok")
        store.conn.execute(
            "UPDATE seller_state SET last_success_at = ? WHERE seller = 'old'",
            ((dt.date.today() - dt.timedelta(days=100)).isoformat() + "T12:00:00",),
        )
        store.conn.commit()
        row = store.coverage_report(["old"])[0]
        assert row["days_since"] == 100
        assert row["status"] == "gap"

    def test_report_sorts_worst_first(self, store):
        for name, days in [("fresh", 1), ("lost", 120), ("edge", 80), ("drifting", 50)]:
            store.mark_seller_attempt(name, pages=1, new_rows=1, status="ok")
            store.conn.execute(
                "UPDATE seller_state SET last_success_at = ? WHERE seller = ?",
                ((dt.date.today() - dt.timedelta(days=days)).isoformat() + "T12:00:00",
                 name),
            )
        store.conn.commit()
        order = [r["seller"] for r in
                 store.coverage_report(["fresh", "lost", "edge", "drifting"])]
        assert order == ["lost", "edge", "drifting", "fresh"]

    def test_consecutive_blocks_are_counted(self, store):
        for _ in range(3):
            store.mark_seller_attempt("x", pages=0, new_rows=0, status="blocked")
        assert store.coverage_report(["x"])[0]["blocks"] == 3
        store.mark_seller_attempt("x", pages=1, new_rows=2, status="ok")
        assert store.coverage_report(["x"])[0]["blocks"] == 0

    def test_a_permanently_blocked_seller_is_surfaced(self, store, capsys):
        """It never succeeds, so it stays "pending" -- it must still be flagged."""
        from ebayparts.cli import print_coverage_warnings

        for _ in range(4):
            store.mark_seller_attempt("wall", pages=0, new_rows=0, status="blocked")
        report = store.coverage_report(["wall"])
        assert report[0]["status"] == "pending"

        print_coverage_warnings(report, lookback_days=90)
        out = capsys.readouterr().out
        assert "wall" in out
        assert "blocked" in out

    def test_healthy_panel_prints_nothing(self, store, capsys):
        from ebayparts.cli import print_coverage_warnings

        store.mark_seller_attempt("good", pages=1, new_rows=1, status="ok")
        print_coverage_warnings(store.coverage_report(["good"]), lookback_days=90)
        assert capsys.readouterr().out == ""


class TestSilentBlockDetection:
    """An unrecognised challenge page must not be recorded as a clean run."""

    def _fetcher_returning(self, html):
        class Fake:
            requested: list = []

            def get(self, url):
                self.requested.append(url)
                return Response(url=url, html=html)

            def close(self): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
        return Fake()

    def test_empty_non_results_page_counts_as_blocked(self, settings, store):
        challenge = (__import__("pathlib").Path(__file__).parent
                     / "fixtures" / "blocked_challenge.html").read_text(encoding="utf-8")
        fake = self._fetcher_returning(challenge)
        result = scrape_seller(
            Seller(user="quiet"), settings, fake, store,
            pacing=Pacing(pages_per_seller=5), budget=DayBudget(store, limit=50),
        )
        assert result.status == "blocked"
        # and crucially, coverage must not now believe this seller is fresh
        assert store.coverage_report(["quiet"])[0]["status"] == "pending"

    def test_a_genuinely_empty_store_is_not_called_blocked(self, settings, store):
        """Real results markup with zero items is an empty store, not a block."""
        empty = ("<html><body><ul class='srp-results'>"
                 "<li class='s-item'></li></ul>"
                 "<div class='srp-river'></div></body></html>")
        fake = self._fetcher_returning(empty)
        result = scrape_seller(
            Seller(user="empty"), settings, fake, store,
            pacing=Pacing(pages_per_seller=5), budget=DayBudget(store, limit=50),
        )
        assert result.status == "ok"
        assert result.rows_found == 0
