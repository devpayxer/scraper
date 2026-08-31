import datetime as dt

from ebayparts.normalize import (
    extract_item_id, normalize_condition, parse_money, parse_quantity_sold,
    parse_shipping, parse_sold_date,
)

TODAY = dt.date(2026, 8, 31)


class TestMoney:
    def test_plain(self):
        assert parse_money("$84.15") == (84.15, "USD")

    def test_thousands(self):
        assert parse_money("US $1,234.56") == (1234.56, "USD")

    def test_other_currencies(self):
        assert parse_money("C $45.00") == (45.0, "CAD")
        assert parse_money("£75.00") == (75.0, "GBP")

    def test_european_decimal_comma(self):
        assert parse_money("EUR 9,99") == (9.99, "EUR")

    def test_range_takes_low_end(self):
        assert parse_money("$10.00 to $20.00") == (10.0, "USD")

    def test_missing(self):
        assert parse_money("") == (None, None)
        assert parse_money("Best offer accepted") == (None, None)


class TestShipping:
    def test_paid_with_eta(self):
        assert parse_shipping("+$56.99 delivery in 2-4 days") == (56.99, "paid")

    def test_paid_plain(self):
        assert parse_shipping("+$16.50 delivery") == (16.5, "paid")

    def test_free(self):
        assert parse_shipping("Free delivery") == (0.0, "free")
        assert parse_shipping("Free shipping") == (0.0, "free")

    def test_unspecified(self):
        assert parse_shipping("Shipping not specified") == (None, "unspecified")

    def test_unknown(self):
        assert parse_shipping("") == (None, "unknown")


class TestSoldDate:
    def test_with_year(self):
        assert parse_sold_date("Sold  Aug 31, 2026", TODAY) == dt.date(2026, 8, 31)

    def test_day_first(self):
        assert parse_sold_date("Sold 12 Jun 2026", TODAY) == dt.date(2026, 6, 12)

    def test_missing_year_never_lands_in_the_future(self):
        # December has not happened yet in 2026, so it must resolve to 2025
        assert parse_sold_date("Sold Dec 30", TODAY) == dt.date(2025, 12, 30)

    def test_missing_year_uses_current_when_past(self):
        assert parse_sold_date("Sold Jul 4", TODAY) == dt.date(2026, 7, 4)

    def test_garbage(self):
        assert parse_sold_date("Sold recently", TODAY) is None


def test_quantity():
    assert parse_quantity_sold("3 sold") == 3
    assert parse_quantity_sold("Buy It Now") == 1


def test_condition():
    assert normalize_condition("Pre-Owned") == "Pre-Owned"
    assert normalize_condition("Used") == "Pre-Owned"
    assert normalize_condition("Open Box") == "New (other)"
    assert normalize_condition("For parts or not working") == "For parts / not working"


def test_item_id():
    assert extract_item_id("https://www.ebay.com/itm/126742318899?hash=x") == "126742318899"
    assert extract_item_id("https://www.ebay.com/itm/some-slug/126742318899") == "126742318899"
    assert extract_item_id("https://www.ebay.com/sch/i.html?_nkw=x") is None
