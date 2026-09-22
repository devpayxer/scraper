"""Inferring sales from listings that stop appearing.

The whole method rests on this: a used part is a quantity-1 listing, so when it
disappears it sold. These tests pin down the grading, because counting an
expired listing as a sale would quietly inflate every number in the report.
"""
from __future__ import annotations

import datetime as dt

import pytest

from ebayparts.ebayapi import ItemSummary
from ebayparts.store import Store
from ebayparts.watch import (
    classify_disappearance, detect_disappearances, record_snapshot,
)

TODAY = dt.date(2026, 9, 22)


class Row(dict):
    def __getitem__(self, key):
        return self.get(key)


def item(item_id="1", title="2015-2018 FORD F-150 LEFT HEADLIGHT OEM",
         price=212.0, end_days=30) -> ItemSummary:
    end = None
    if end_days is not None:
        end = dt.datetime.combine(TODAY + dt.timedelta(days=end_days), dt.time())
    return ItemSummary(item_id=item_id, title=title, price=price, currency="USD",
                       condition="Used", seller="spartan_auto", shipping_cost=0.0,
                       item_end_date=end, item_web_url=f"https://ebay.com/itm/{item_id}")


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "watch.db") as s:
        yield s


class TestClassify:
    def test_vanishing_long_before_the_end_date_is_a_sale(self):
        end = (TODAY + dt.timedelta(days=30)).isoformat()
        assert classify_disappearance(Row(item_end_date=end), TODAY) == ("likely", True)

    def test_vanishing_at_the_end_date_is_uncertain(self):
        end = TODAY.isoformat()
        assert classify_disappearance(Row(item_end_date=end), TODAY) == ("uncertain", True)

    def test_already_past_its_end_date_is_expired_and_not_a_sale(self):
        end = (TODAY - dt.timedelta(days=1)).isoformat()
        assert classify_disappearance(Row(item_end_date=end), TODAY) == ("expired", False)

    def test_no_end_date_is_counted_but_flagged(self):
        assert classify_disappearance(Row(item_end_date=None), TODAY) == ("uncertain", True)

    def test_unparseable_end_date_does_not_crash(self):
        assert classify_disappearance(Row(item_end_date="not a date"), TODAY)[0] == "uncertain"


class TestSnapshot:
    def test_first_snapshot_adds_everything(self, store):
        seen, added = record_snapshot(store, [item("1"), item("2")], "headlight",
                                      today=TODAY)
        assert (seen, added) == (2, 2)

    def test_second_snapshot_updates_rather_than_duplicates(self, store):
        record_snapshot(store, [item("1")], "headlight", today=TODAY)
        seen, added = record_snapshot(store, [item("1")], "headlight",
                                      today=TODAY + dt.timedelta(days=1))
        assert (seen, added) == (1, 0)
        row = store.query("SELECT times_seen FROM active_listings")[0]
        assert row["times_seen"] == 2

    def test_price_changes_are_tracked(self, store):
        record_snapshot(store, [item("1", price=212.0)], "headlight", today=TODAY)
        record_snapshot(store, [item("1", price=180.0)], "headlight",
                        today=TODAY + dt.timedelta(days=1))
        assert store.query("SELECT price FROM active_listings")[0]["price"] == 180.0


class TestDetectDisappearances:
    def test_nothing_gone_on_the_same_day(self, store):
        record_snapshot(store, [item("1")], "headlight", today=TODAY)
        assert detect_disappearances(store, "headlight", today=TODAY) == (0, 0)

    def test_a_vanished_listing_becomes_a_sale(self, store):
        record_snapshot(store, [item("1"), item("2")], "headlight", today=TODAY)
        tomorrow = TODAY + dt.timedelta(days=1)
        record_snapshot(store, [item("2")], "headlight", today=tomorrow)

        gone, sales = detect_disappearances(store, "headlight", today=tomorrow)
        assert (gone, sales) == (1, 1)
        rows = store.query("SELECT * FROM listings")
        assert len(rows) == 1
        assert rows[0]["source"] == "inferred"
        assert rows[0]["confidence"] == "likely"
        assert rows[0]["make"] == "Ford"
        assert rows[0]["part_category"] == "Headlight"

    def test_an_expired_listing_is_not_counted_as_a_sale(self, store):
        record_snapshot(store, [item("1", end_days=0)], "headlight", today=TODAY)
        later = TODAY + dt.timedelta(days=2)
        record_snapshot(store, [], "headlight", today=later)

        gone, sales = detect_disappearances(store, "headlight", today=later)
        assert gone == 1, "it did disappear"
        assert sales == 0, "but it expired rather than sold"
        assert store.count() == 0

    def test_a_listing_is_only_banked_once(self, store):
        record_snapshot(store, [item("1")], "headlight", today=TODAY)
        tomorrow = TODAY + dt.timedelta(days=1)
        record_snapshot(store, [], "headlight", today=tomorrow)
        assert detect_disappearances(store, "headlight", today=tomorrow) == (1, 1)
        assert detect_disappearances(store, "headlight", today=tomorrow) == (0, 0)
        assert store.count() == 1

    def test_queries_do_not_interfere(self, store):
        record_snapshot(store, [item("1")], "headlight", today=TODAY)
        record_snapshot(store, [item("9")], "bumper", today=TODAY)
        tomorrow = TODAY + dt.timedelta(days=1)
        record_snapshot(store, [item("9")], "bumper", today=tomorrow)
        assert detect_disappearances(store, "bumper", today=tomorrow) == (0, 0)
