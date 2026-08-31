"""Aggregations over the collected sales.

Answers the four questions this tool exists for:
  * which kind of part sells most            -> top_categories()
  * which car brand / model is hot           -> top_makes(), top_models()
  * what does it sell for                    -> price stats on every breakdown
  * what is heating up or cooling off        -> momentum()
"""
from __future__ import annotations

import datetime as dt
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from .store import Store

SELECT_COLUMNS = (
    "sold_date, price, shipping_cost, shipping_status, total_price, quantity_sold, "
    "make, model, year_from, year_to, part_category, part_group, seller, condition, "
    "is_oem, title"
)


@dataclass
class Row:
    sold_date: dt.date | None
    price: float | None
    shipping_cost: float | None
    shipping_status: str | None
    total_price: float | None
    quantity_sold: int
    make: str | None
    model: str | None
    year_from: int | None
    year_to: int | None
    part_category: str | None
    part_group: str | None
    seller: str | None
    condition: str | None
    is_oem: bool
    title: str

    @property
    def revenue(self) -> float:
        return (self.price or 0.0) * max(1, self.quantity_sold)

    @property
    def gross(self) -> float:
        """Revenue including what the buyer paid for shipping."""
        return (self.total_price or self.price or 0.0) * max(1, self.quantity_sold)

    @property
    def vehicle(self) -> str | None:
        if not self.make:
            return None
        return f"{self.make} {self.model}" if self.model else self.make

    @property
    def generation(self) -> str | None:
        if not self.make or not self.model or not self.year_from:
            return None
        span = (f"{self.year_from}-{self.year_to}"
                if self.year_to and self.year_to != self.year_from else str(self.year_from))
        return f"{self.make} {self.model} {span}"


def load_rows(store: Store, *, days: int | None = 90, today: dt.date | None = None,
              sellers: Sequence[str] | None = None) -> list[Row]:
    """Pull the analysis window into memory. Rows without a price are dropped."""
    today = today or dt.date.today()
    sql = f"SELECT {SELECT_COLUMNS} FROM listings WHERE price IS NOT NULL"
    params: list[Any] = []
    if days:
        cutoff = (today - dt.timedelta(days=days)).isoformat()
        sql += " AND sold_date IS NOT NULL AND sold_date >= ?"
        params.append(cutoff)
    if sellers:
        sql += f" AND seller IN ({', '.join('?' for _ in sellers)})"
        params.extend(sellers)

    rows: list[Row] = []
    for raw in store.query(sql, tuple(params)):
        sold = raw["sold_date"]
        rows.append(
            Row(
                sold_date=dt.date.fromisoformat(sold) if sold else None,
                price=raw["price"],
                shipping_cost=raw["shipping_cost"],
                shipping_status=raw["shipping_status"],
                total_price=raw["total_price"],
                quantity_sold=raw["quantity_sold"] or 1,
                make=raw["make"],
                model=raw["model"],
                year_from=raw["year_from"],
                year_to=raw["year_to"],
                part_category=raw["part_category"],
                part_group=raw["part_group"],
                seller=raw["seller"],
                condition=raw["condition"],
                is_oem=bool(raw["is_oem"]),
                title=raw["title"],
            )
        )
    return rows


# --------------------------------------------------------------- statistics

def _percentile(values: Sequence[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 2)
    index = (len(ordered) - 1) * pct
    low, high = int(index), min(int(index) + 1, len(ordered) - 1)
    weight = index - low
    return round(ordered[low] * (1 - weight) + ordered[high] * weight, 2)


def price_stats(rows: Iterable[Row]) -> dict[str, Any]:
    rows = list(rows)
    prices = [r.price for r in rows if r.price is not None]
    shipping = [r.shipping_cost for r in rows if r.shipping_cost is not None]
    units = sum(max(1, r.quantity_sold) for r in rows)
    return {
        "listings": len(rows),
        "units": units,
        "revenue": round(sum(r.revenue for r in rows), 2),
        "gross_with_shipping": round(sum(r.gross for r in rows), 2),
        "avg_price": round(statistics.fmean(prices), 2) if prices else None,
        "median_price": round(statistics.median(prices), 2) if prices else None,
        "min_price": round(min(prices), 2) if prices else None,
        "max_price": round(max(prices), 2) if prices else None,
        "p25_price": _percentile(prices, 0.25),
        "p75_price": _percentile(prices, 0.75),
        "avg_shipping": round(statistics.fmean(shipping), 2) if shipping else None,
        "free_shipping_pct": (
            round(100 * sum(1 for r in rows if r.shipping_status == "free") / len(rows), 1)
            if rows else None
        ),
    }


def group_by(rows: Iterable[Row], key: Callable[[Row], Any],
             *, min_listings: int = 1, limit: int | None = None,
             sort_key: str = "units") -> list[dict[str, Any]]:
    """Bucket rows by `key` and attach price stats to every bucket."""
    buckets: dict[Any, list[Row]] = defaultdict(list)
    for row in rows:
        value = key(row)
        if value is None:
            continue
        buckets[value].append(row)

    output = []
    for value, bucket in buckets.items():
        if len(bucket) < min_listings:
            continue
        stats = price_stats(bucket)
        stats["key"] = value
        output.append(stats)

    output.sort(key=lambda item: (item.get(sort_key) or 0, item["revenue"]), reverse=True)
    return output[:limit] if limit else output


# ---------------------------------------------------------------- rankings

def top_categories(rows, limit=25, sort_key="units"):
    return group_by(rows, lambda r: r.part_category, limit=limit, sort_key=sort_key)


def top_part_groups(rows, limit=25):
    return group_by(rows, lambda r: r.part_group, limit=limit)


def top_makes(rows, limit=25, sort_key="units"):
    return group_by(rows, lambda r: r.make, limit=limit, sort_key=sort_key)


def top_models(rows, limit=30, min_listings=2, sort_key="units"):
    return group_by(rows, lambda r: r.vehicle, limit=limit,
                    min_listings=min_listings, sort_key=sort_key)


def top_generations(rows, limit=30, min_listings=2):
    return group_by(rows, lambda r: r.generation, limit=limit, min_listings=min_listings)


def top_combos(rows, limit=30, min_listings=2):
    """The actionable unit: "BMW 3 Series -- Headlight"."""
    def key(row: Row):
        if not row.make or not row.part_category:
            return None
        return f"{row.vehicle} — {row.part_category}"
    return group_by(rows, key, limit=limit, min_listings=min_listings)


def top_sellers(rows, limit=60):
    return group_by(rows, lambda r: r.seller, limit=limit)


def by_condition(rows):
    return group_by(rows, lambda r: r.condition)


def by_model_year(rows, limit=30):
    return group_by(rows, lambda r: r.year_from, limit=limit)


# ------------------------------------------------------------------ trends

def weekly_trend(rows: Iterable[Row]) -> list[dict[str, Any]]:
    buckets: dict[dt.date, list[Row]] = defaultdict(list)
    for row in rows:
        if row.sold_date:
            week = row.sold_date - dt.timedelta(days=row.sold_date.weekday())
            buckets[week].append(row)
    trend = []
    for week in sorted(buckets):
        stats = price_stats(buckets[week])
        stats["key"] = week.isoformat()
        trend.append(stats)
    return trend


def momentum(
    rows: Iterable[Row],
    key: Callable[[Row], Any],
    *,
    today: dt.date | None = None,
    recent_days: int = 30,
    min_units: int = 3,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """Compare the last `recent_days` against the equivalent prior window.

    `change_pct` is the honest signal for "what is heating up": a part with 40
    units last month against 12 the month before is trending, one flat at 40 is
    merely popular.
    """
    today = today or dt.date.today()
    recent_start = today - dt.timedelta(days=recent_days)
    prior_start = recent_start - dt.timedelta(days=recent_days)

    recent: dict[Any, list[Row]] = defaultdict(list)
    prior: dict[Any, list[Row]] = defaultdict(list)
    for row in rows:
        value = key(row)
        if value is None or row.sold_date is None:
            continue
        if row.sold_date >= recent_start:
            recent[value].append(row)
        elif row.sold_date >= prior_start:
            prior[value].append(row)

    output = []
    for value in set(recent) | set(prior):
        recent_units = sum(max(1, r.quantity_sold) for r in recent.get(value, []))
        prior_units = sum(max(1, r.quantity_sold) for r in prior.get(value, []))
        if recent_units + prior_units < min_units:
            continue
        if prior_units:
            change = round(100 * (recent_units - prior_units) / prior_units, 1)
        else:
            change = None  # brand new to the data; no baseline to divide by
        recent_prices = [r.price for r in recent.get(value, []) if r.price is not None]
        prior_prices = [r.price for r in prior.get(value, []) if r.price is not None]
        output.append({
            "key": value,
            "recent_units": recent_units,
            "prior_units": prior_units,
            "delta_units": recent_units - prior_units,
            "change_pct": change,
            "recent_median": round(statistics.median(recent_prices), 2) if recent_prices else None,
            "prior_median": round(statistics.median(prior_prices), 2) if prior_prices else None,
            "recent_revenue": round(sum(r.revenue for r in recent.get(value, [])), 2),
        })

    output.sort(key=lambda item: (item["delta_units"], item["recent_units"]), reverse=True)
    return output[:limit]


# ------------------------------------------------------------- data quality

def coverage(rows: Sequence[Row]) -> dict[str, Any]:
    """How much of the data the taxonomies actually recognised."""
    total = len(rows) or 1
    return {
        "rows": len(rows),
        "with_make_pct": round(100 * sum(1 for r in rows if r.make) / total, 1),
        "with_model_pct": round(100 * sum(1 for r in rows if r.model) / total, 1),
        "with_category_pct": round(100 * sum(1 for r in rows if r.part_category) / total, 1),
        "with_date_pct": round(100 * sum(1 for r in rows if r.sold_date) / total, 1),
        "with_shipping_pct": round(
            100 * sum(1 for r in rows if r.shipping_cost is not None) / total, 1),
    }


def unmatched_titles(rows: Sequence[Row], field_name: str = "part_category",
                     limit: int = 40) -> list[str]:
    """Titles the taxonomy missed -- the to-do list for categories.yml."""
    return [r.title for r in rows if getattr(r, field_name) is None][:limit]


@dataclass
class Report:
    window_days: int
    generated_at: dt.datetime
    totals: dict[str, Any]
    coverage: dict[str, Any]
    categories: list[dict[str, Any]] = field(default_factory=list)
    groups: list[dict[str, Any]] = field(default_factory=list)
    makes: list[dict[str, Any]] = field(default_factory=list)
    models: list[dict[str, Any]] = field(default_factory=list)
    generations: list[dict[str, Any]] = field(default_factory=list)
    combos: list[dict[str, Any]] = field(default_factory=list)
    conditions: list[dict[str, Any]] = field(default_factory=list)
    model_years: list[dict[str, Any]] = field(default_factory=list)
    sellers: list[dict[str, Any]] = field(default_factory=list)
    trend: list[dict[str, Any]] = field(default_factory=list)
    hot_categories: list[dict[str, Any]] = field(default_factory=list)
    hot_models: list[dict[str, Any]] = field(default_factory=list)
    uncategorized: list[str] = field(default_factory=list)


def build_report(rows: Sequence[Row], *, window_days: int = 90,
                 today: dt.date | None = None) -> Report:
    today = today or dt.date.today()
    return Report(
        window_days=window_days,
        generated_at=dt.datetime.now(),
        totals=price_stats(rows),
        coverage=coverage(rows),
        categories=top_categories(rows, limit=30),
        groups=top_part_groups(rows, limit=20),
        makes=top_makes(rows, limit=25),
        models=top_models(rows, limit=30),
        generations=top_generations(rows, limit=25),
        combos=top_combos(rows, limit=30),
        conditions=by_condition(rows),
        model_years=by_model_year(rows, limit=30),
        sellers=top_sellers(rows),
        trend=weekly_trend(rows),
        hot_categories=momentum(rows, lambda r: r.part_category, today=today),
        hot_models=momentum(rows, lambda r: r.vehicle, today=today),
        uncategorized=unmatched_titles(rows),
    )
