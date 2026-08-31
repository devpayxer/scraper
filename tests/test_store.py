import datetime as dt

import pytest

from ebayparts.parse import Listing
from ebayparts.store import Store, row_key


def make_listing(**overrides) -> Listing:
    base = dict(
        item_id="126742318899",
        title="2015-2018 FORD F-150 LEFT HEADLIGHT OEM",
        sold_date=dt.date(2026, 8, 31),
        price=212.0,
        shipping_cost=0.0,
        shipping_status="free",
        total_price=212.0,
        seller="spartan_auto",
    )
    base.update(overrides)
    return Listing(**base)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "test.db") as s:
        yield s


def test_insert_then_reinsert_is_idempotent(store):
    listing = make_listing()
    assert store.upsert_many([listing]) == (1, 1)
    assert store.upsert_many([listing]) == (1, 0)
    assert store.count() == 1


def test_same_item_sold_twice_is_two_rows(store):
    """Multi-quantity listings sell repeatedly under one item id."""
    store.upsert_many([
        make_listing(sold_date=dt.date(2026, 8, 30)),
        make_listing(sold_date=dt.date(2026, 8, 31)),
    ])
    assert store.count() == 2


def test_same_item_different_price_is_two_rows(store):
    store.upsert_many([make_listing(price=212.0), make_listing(price=180.0)])
    assert store.count() == 2


def test_row_key_is_stable(store):
    assert row_key(make_listing()) == row_key(make_listing())


def test_reindex_recomputes_from_the_stored_title(store):
    store.upsert_many([make_listing()])
    store.conn.execute("UPDATE listings SET make=NULL, part_category=NULL")
    store.conn.commit()
    assert store.reindex() == 1
    row = store.query("SELECT make, part_category FROM listings")[0]
    assert row["make"] == "Ford"
    assert row["part_category"] == "Headlight"


def test_date_span_and_sellers(store):
    store.upsert_many([
        make_listing(sold_date=dt.date(2026, 6, 1)),
        make_listing(sold_date=dt.date(2026, 8, 31)),
    ])
    assert store.date_span() == ("2026-06-01", "2026-08-31")
    assert store.sellers() == ["spartan_auto"]
