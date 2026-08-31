"""SQLite persistence.

One row per (item, sale date, price). Re-running a scrape is idempotent: the
same sale updates `last_seen` rather than duplicating, so you can run this
nightly forever and the 90-day window just rolls.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from .parse import Listing

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    row_key             TEXT PRIMARY KEY,
    item_id             TEXT NOT NULL,
    title               TEXT NOT NULL,
    url                 TEXT,
    seller              TEXT,
    seller_feedback_pct REAL,
    sold_date           TEXT,
    price               REAL,
    currency            TEXT,
    original_price      REAL,
    shipping_cost       REAL,
    shipping_status     TEXT,
    total_price         REAL,
    quantity_sold       INTEGER DEFAULT 1,
    condition           TEXT,
    location            TEXT,
    image_url           TEXT,
    year_from           INTEGER,
    year_to             INTEGER,
    make                TEXT,
    model               TEXT,
    part_category       TEXT,
    part_group          TEXT,
    is_oem              INTEGER DEFAULT 0,
    side                TEXT,
    position            TEXT,
    source_url          TEXT,
    first_seen          TEXT,
    last_seen           TEXT
);
CREATE INDEX IF NOT EXISTS idx_listings_sold_date ON listings(sold_date);
CREATE INDEX IF NOT EXISTS idx_listings_seller    ON listings(seller);
CREATE INDEX IF NOT EXISTS idx_listings_make      ON listings(make, model);
CREATE INDEX IF NOT EXISTS idx_listings_category  ON listings(part_category);
CREATE INDEX IF NOT EXISTS idx_listings_item      ON listings(item_id);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT,
    finished_at TEXT,
    seller      TEXT,
    pages       INTEGER,
    rows_found  INTEGER,
    rows_new    INTEGER,
    status      TEXT,
    note        TEXT
);
"""

COLUMNS = [
    "row_key", "item_id", "title", "url", "seller", "seller_feedback_pct", "sold_date",
    "price", "currency", "original_price", "shipping_cost", "shipping_status",
    "total_price", "quantity_sold", "condition", "location", "image_url",
    "year_from", "year_to", "make", "model", "part_category", "part_group",
    "is_oem", "side", "position", "source_url", "first_seen", "last_seen",
]

# Columns recomputed from the title. `reindex` rewrites exactly these, so
# editing categories.yml/vehicles.yml never needs a re-scrape.
DERIVED_COLUMNS = [
    "year_from", "year_to", "make", "model", "part_category", "part_group",
    "is_oem", "side", "position",
]


def row_key(listing: Listing) -> str:
    """Stable identity for one sale.

    Item id alone is not enough: multi-quantity listings sell repeatedly under
    the same id, and eBay shows each sale separately.
    """
    parts = [
        listing.item_id,
        listing.sold_date.isoformat() if listing.sold_date else "",
        f"{listing.price:.2f}" if listing.price is not None else "",
    ]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ----------------------------------------------------------- lifecycle
    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc_info) -> None:
        self.conn.commit()
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # --------------------------------------------------------------- write
    def upsert_many(self, listings: Iterable[Listing]) -> tuple[int, int]:
        """Insert listings. Returns (seen, newly_inserted)."""
        now = dt.datetime.utcnow().isoformat(timespec="seconds")
        seen = new = 0
        placeholders = ", ".join("?" for _ in COLUMNS)
        updates = ", ".join(f"{c}=excluded.{c}" for c in COLUMNS if c not in
                            ("row_key", "first_seen"))
        sql = (
            f"INSERT INTO listings ({', '.join(COLUMNS)}) VALUES ({placeholders}) "
            f"ON CONFLICT(row_key) DO UPDATE SET {updates}"
        )
        with self.transaction() as conn:
            for listing in listings:
                seen += 1
                key = row_key(listing)
                exists = conn.execute(
                    "SELECT 1 FROM listings WHERE row_key = ?", (key,)
                ).fetchone()
                if not exists:
                    new += 1
                data = listing.as_dict()
                data.update(row_key=key, first_seen=now, last_seen=now,
                            is_oem=int(listing.is_oem))
                conn.execute(sql, [data.get(c) for c in COLUMNS])
        return seen, new

    def record_run(self, **kwargs: Any) -> None:
        keys = ["started_at", "finished_at", "seller", "pages", "rows_found",
                "rows_new", "status", "note"]
        with self.transaction() as conn:
            conn.execute(
                f"INSERT INTO scrape_runs ({', '.join(keys)}) "
                f"VALUES ({', '.join('?' for _ in keys)})",
                [kwargs.get(k) for k in keys],
            )

    def reindex(self) -> int:
        """Recompute every title-derived column in place.

        Run this after editing categories.yml or vehicles.yml -- it is the whole
        point of storing the raw title.
        """
        from .enrich import enrich_title

        rows = self.conn.execute("SELECT row_key, title FROM listings").fetchall()
        sql = (f"UPDATE listings SET {', '.join(f'{c}=?' for c in DERIVED_COLUMNS)} "
               f"WHERE row_key=?")
        with self.transaction() as conn:
            for row in rows:
                e = enrich_title(row["title"])
                conn.execute(sql, [
                    e.year_from, e.year_to, e.make, e.model, e.part_category,
                    e.part_group, int(e.is_oem), e.side, e.position, row["row_key"],
                ])
        return len(rows)

    # ---------------------------------------------------------------- read
    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]

    def date_span(self) -> tuple[str | None, str | None]:
        row = self.conn.execute(
            "SELECT MIN(sold_date), MAX(sold_date) FROM listings WHERE sold_date IS NOT NULL"
        ).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def sellers(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT seller FROM listings WHERE seller IS NOT NULL ORDER BY seller"
        ).fetchall()
        return [r[0] for r in rows]
