import datetime as dt
from pathlib import Path

import pytest

from ebayparts.parse import parse_search_page, result_count

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = dt.date(2026, 8, 31)


@pytest.fixture(params=["sold_classic.html", "sold_new_layout.html"])
def page(request):
    return (FIXTURES / request.param).read_text(encoding="utf-8")


def test_every_layout_yields_listings(page):
    assert parse_search_page(page, today=TODAY)


def test_no_field_is_silently_missing(page):
    """A layout change usually shows up as a whole column going empty."""
    for item in parse_search_page(page, today=TODAY):
        assert item.item_id and item.item_id.isdigit()
        assert item.title
        assert item.price is not None
        assert item.sold_date is not None
        assert item.shipping_status in {"free", "paid", "unspecified", "unknown"}
        assert item.condition
        assert item.seller == "spartan_auto"


class TestClassicLayout:
    @pytest.fixture
    def rows(self):
        html = (FIXTURES / "sold_classic.html").read_text(encoding="utf-8")
        return parse_search_page(html, today=TODAY)

    def test_promo_card_is_dropped(self, rows):
        # the fixture contains a "Shop on eBay" house ad
        assert len(rows) == 3
        assert all("Shop on eBay" not in r.title for r in rows)

    def test_first_row_matches_the_screenshot(self, rows):
        row = rows[0]
        assert row.item_id == "126742318899"
        assert row.price == 84.15
        assert row.original_price == 99.00
        assert row.shipping_cost == 56.99
        assert row.total_price == 141.14
        assert row.sold_date == dt.date(2026, 8, 31)
        assert row.make == "Chevrolet" and row.model == "Tahoe"
        assert row.part_category == "Wheel / Rim"

    def test_result_count_header(self):
        html = (FIXTURES / "sold_classic.html").read_text(encoding="utf-8")
        assert result_count(html) == 1482


class TestNewLayout:
    @pytest.fixture
    def rows(self):
        html = (FIXTURES / "sold_new_layout.html").read_text(encoding="utf-8")
        return parse_search_page(html, today=TODAY)

    def test_free_shipping(self, rows):
        row = rows[0]
        assert (row.shipping_cost, row.shipping_status) == (0.0, "free")
        assert row.total_price == row.price

    def test_condition_without_a_condition_class(self, rows):
        assert all(r.condition == "Pre-Owned" for r in rows)


def test_block_page_yields_nothing():
    assert parse_search_page("<html><body>Pardon Our Interruption</body></html>") == []
