"""Turn snapshots of active listings into a sales signal.

The Browse API only shows what is for sale right now. So we record what is
listed each day, and when an item we were watching stops appearing, we treat
that disappearance as a sale.

For used car parts this is a strong proxy, not a wild guess: these are almost
all quantity-1 listings, so the listing ends when the part sells. We still
grade every inference rather than pretending it is certain:

    likely    the listing vanished well before its end date -> it sold
    uncertain it vanished near its end date -> it may have simply expired
    expired   it is past its end date -> treat as expired, not a sale

Only `likely` rows are counted as sales by default. `expired` ones are recorded
but excluded, so an honest number is available either way.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from .ebayapi import EbayBrowseClient, ItemSummary
from .enrich import enrich_title
from .normalize import normalize_condition
from .parse import Listing
from .store import Store

log = logging.getLogger(__name__)

# A listing that disappears with more than this long left to run almost
# certainly sold; one vanishing inside this window may have just expired.
SOLD_MARGIN = dt.timedelta(days=1)


@dataclass
class WatchResult:
    query: str
    seen: int = 0
    added: int = 0
    disappeared: int = 0
    inferred_sales: int = 0
    api_calls: int = 0
    error: str = ""


def classify_disappearance(row, today: dt.date) -> tuple[str, bool]:
    """Return (confidence, counts_as_sale) for a listing that stopped appearing."""
    end_raw = row["item_end_date"]
    if not end_raw:
        # No end date published. Most used-part listings are quantity 1, so a
        # disappearance is more likely a sale than not -- but say so honestly.
        return "uncertain", True
    try:
        end = dt.datetime.fromisoformat(str(end_raw)).date()
    except ValueError:
        return "uncertain", True

    if today > end:
        return "expired", False
    if end - today > SOLD_MARGIN:
        return "likely", True
    return "uncertain", True


def _to_listing(row, *, sold_date: dt.date, confidence: str) -> Listing:
    title = row["title"]
    enriched = enrich_title(title)
    price = row["price"]
    shipping = row["shipping_cost"]
    total = round(price + (shipping or 0.0), 2) if price is not None else None
    return Listing(
        item_id=str(row["item_id"]),
        title=title,
        url=row["url"],
        seller=row["seller"],
        seller_feedback_pct=row["seller_feedback_pct"],
        sold_date=sold_date,
        price=price,
        currency=row["currency"],
        shipping_cost=shipping,
        shipping_status=("free" if shipping == 0 else
                         "paid" if shipping is not None else "unknown"),
        total_price=total,
        condition=normalize_condition(row["condition"]),
        location=row["location"],
        image_url=row["image_url"],
        year_from=enriched.year_from,
        year_to=enriched.year_to,
        make=enriched.make,
        model=enriched.model,
        part_category=enriched.part_category,
        part_group=enriched.part_group,
        is_oem=enriched.is_oem,
        side=enriched.side,
        position=enriched.position,
        source="inferred",
        confidence=confidence,
        source_url="ebay-browse-api",
    )


def record_snapshot(store: Store, items: list[ItemSummary], query: str,
                    *, today: dt.date | None = None) -> tuple[int, int]:
    """Upsert everything currently listed. Returns (seen, newly_added)."""
    today = today or dt.date.today()
    stamp = today.isoformat()
    added = 0
    with store.transaction() as conn:
        for item in items:
            existing = conn.execute(
                "SELECT item_id FROM active_listings WHERE item_id = ?",
                (item.item_id,)).fetchone()
            if existing is None:
                added += 1
            conn.execute(
                """INSERT INTO active_listings
                       (item_id, title, price, currency, shipping_cost, condition,
                        seller, seller_feedback_pct, item_end_date, url, image_url,
                        location, query, first_seen, last_seen, times_seen, gone)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,0)
                   ON CONFLICT(item_id) DO UPDATE SET
                       price = excluded.price,
                       shipping_cost = excluded.shipping_cost,
                       last_seen = excluded.last_seen,
                       item_end_date = COALESCE(excluded.item_end_date,
                                                active_listings.item_end_date),
                       times_seen = active_listings.times_seen + 1,
                       gone = 0""",
                (item.item_id, item.title, item.price, item.currency,
                 item.shipping_cost, item.condition, item.seller,
                 item.seller_feedback_pct,
                 item.item_end_date.date().isoformat() if item.item_end_date else None,
                 item.item_web_url, item.image_url, item.location, query,
                 stamp, stamp),
            )
    return len(items), added


def detect_disappearances(store: Store, query: str, *,
                          today: dt.date | None = None,
                          min_days_watched: int = 1) -> tuple[int, int]:
    """Find watched listings that stopped appearing and record inferred sales.

    Returns (disappeared, counted_as_sales).
    """
    today = today or dt.date.today()
    stamp = today.isoformat()
    rows = store.query(
        "SELECT * FROM active_listings "
        "WHERE query = ? AND gone = 0 AND last_seen < ? AND times_seen >= ?",
        (query, stamp, min_days_watched),
    )
    if not rows:
        return 0, 0

    sales: list[Listing] = []
    for row in rows:
        confidence, counts = classify_disappearance(row, today)
        listing = _to_listing(row, sold_date=today, confidence=confidence)
        if counts:
            sales.append(listing)
        else:
            log.debug("%s expired rather than sold", row["item_id"])

    if sales:
        store.upsert_many(sales)

    with store.transaction() as conn:
        conn.executemany(
            "UPDATE active_listings SET gone = 1 WHERE item_id = ?",
            [(r["item_id"],) for r in rows],
        )
    return len(rows), len(sales)


def watch_query(client: EbayBrowseClient, store: Store, query: str, *,
                category_ids: str | int | None = None, max_items: int = 600,
                today: dt.date | None = None) -> WatchResult:
    """One query: snapshot what is listed, then bank what vanished."""
    result = WatchResult(query=query)
    before = client.calls_made
    try:
        items = list(client.iter_items(query=query, category_ids=category_ids,
                                       max_items=max_items))
    except Exception as exc:
        result.error = str(exc)
        result.api_calls = client.calls_made - before
        return result

    result.seen, result.added = record_snapshot(store, items, query, today=today)
    result.disappeared, result.inferred_sales = detect_disappearances(
        store, query, today=today)
    result.api_calls = client.calls_made - before

    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO watch_runs (ran_at, query, seen, added, disappeared, api_calls)"
            " VALUES (?,?,?,?,?,?)",
            (dt.datetime.now().isoformat(timespec="seconds"), query, result.seen,
             result.added, result.disappeared, result.api_calls),
        )
    return result
