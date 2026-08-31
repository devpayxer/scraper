import datetime as dt

import pytest

from ebayparts.analyze import (
    Row, build_report, coverage, momentum, price_stats, top_categories, top_makes,
)

TODAY = dt.date(2026, 8, 31)


def row(days_ago: int, price: float, category="Headlight", make="Ford", model="F-150",
        qty=1, shipping=0.0, status="free") -> Row:
    return Row(
        sold_date=TODAY - dt.timedelta(days=days_ago), price=price, shipping_cost=shipping,
        shipping_status=status, total_price=price + shipping, quantity_sold=qty, make=make,
        model=model, year_from=2015, year_to=2018, part_category=category,
        part_group="Lighting", seller="spartan_auto", condition="Pre-Owned", is_oem=True,
        title=f"{category} for {make} {model}",
    )


class TestPriceStats:
    def test_basic(self):
        stats = price_stats([row(1, 100.0), row(2, 200.0), row(3, 300.0)])
        assert stats["units"] == 3
        assert stats["revenue"] == 600.0
        assert stats["median_price"] == 200.0
        assert stats["p25_price"] == 150.0
        assert stats["p75_price"] == 250.0

    def test_quantity_multiplies_units_and_revenue(self):
        stats = price_stats([row(1, 100.0, qty=4)])
        assert (stats["units"], stats["revenue"]) == (4, 400.0)

    def test_shipping_counted_separately_from_revenue(self):
        stats = price_stats([row(1, 100.0, shipping=25.0, status="paid")])
        assert stats["revenue"] == 100.0
        assert stats["gross_with_shipping"] == 125.0
        assert stats["free_shipping_pct"] == 0.0

    def test_empty(self):
        assert price_stats([])["units"] == 0


class TestRankings:
    def test_categories_sorted_by_units(self):
        rows = [row(1, 100, category="Headlight") for _ in range(3)]
        rows += [row(1, 500, category="Transmission")]
        ranked = top_categories(rows)
        assert [r["key"] for r in ranked] == ["Headlight", "Transmission"]
        assert ranked[0]["units"] == 3

    def test_makes(self):
        rows = [row(1, 100, make="Ford"), row(1, 100, make="BMW"), row(1, 100, make="BMW")]
        assert top_makes(rows)[0]["key"] == "BMW"

    def test_rows_without_the_key_are_skipped(self):
        assert top_makes([row(1, 100, make=None)]) == []


class TestMomentum:
    def test_detects_a_rise(self):
        rows = [row(5, 100) for _ in range(10)] + [row(45, 100) for _ in range(5)]
        result = momentum(rows, lambda r: r.part_category, today=TODAY)[0]
        assert result["recent_units"] == 10
        assert result["prior_units"] == 5
        assert result["change_pct"] == 100.0

    def test_detects_a_fall(self):
        rows = [row(5, 100) for _ in range(2)] + [row(45, 100) for _ in range(10)]
        assert momentum(rows, lambda r: r.part_category, today=TODAY)[0]["change_pct"] == -80.0

    def test_no_baseline_reports_none_not_infinity(self):
        rows = [row(5, 100) for _ in range(5)]
        assert momentum(rows, lambda r: r.part_category, today=TODAY)[0]["change_pct"] is None

    def test_low_volume_is_filtered_out(self):
        assert momentum([row(5, 100)], lambda r: r.part_category,
                        today=TODAY, min_units=3) == []


def test_coverage_reports_taxonomy_gaps():
    rows = [row(1, 100), row(1, 100, make=None, model=None)]
    cov = coverage(rows)
    assert cov["with_make_pct"] == 50.0
    assert cov["with_category_pct"] == 100.0


def test_build_report_populates_every_section():
    rows = [row(d % 80, 100 + d) for d in range(60)]
    report = build_report(rows, window_days=90, today=TODAY)
    assert report.totals["units"] == 60
    assert report.categories and report.makes and report.models and report.trend

