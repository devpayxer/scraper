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

-- One line per page actually fetched from eBay (cache hits excluded).
-- This is what the daily budget is counted against, and it is the audit trail
-- if you ever want to prove how little this thing asked for.
CREATE TABLE IF NOT EXISTS fetch_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at TEXT,
    day       TEXT,
    seller    TEXT,
    url       TEXT,
    outcome   TEXT
);
CREATE INDEX IF NOT EXISTS idx_fetch_log_day ON fetch_log(day);

-- Rotation state: who was visited when, so the panel is spread over days
-- instead of hammered in one night.
CREATE TABLE IF NOT EXISTS seller_state (
    seller          TEXT PRIMARY KEY,
    last_attempt_at TEXT,
    last_success_at TEXT,
    last_pages      INTEGER DEFAULT 0,
    last_new_rows   INTEGER DEFAULT 0,
    total_rows      INTEGER DEFAULT 0,
    backfilled      INTEGER DEFAULT 0,
    consecutive_blocks INTEGER DEFAULT 0
);

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

    # ------------------------------------------------------- pacing state
    def record_fetch(self, seller: str | None, url: str, outcome: str) -> None:
        """Log a real network fetch. Cache hits must not be logged."""
        now = dt.datetime.now()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO fetch_log (fetched_at, day, seller, url, outcome) "
                "VALUES (?, ?, ?, ?, ?)",
                (now.isoformat(timespec="seconds"), now.date().isoformat(),
                 seller, url, outcome),
            )

    def pages_fetched_today(self, today: dt.date | None = None) -> int:
        day = (today or dt.date.today()).isoformat()
        row = self.conn.execute(
            "SELECT COUNT(*) FROM fetch_log WHERE day = ? AND outcome != 'cached'", (day,)
        ).fetchone()
        return row[0] if row else 0

    def fetch_history(self, days: int = 14) -> list[sqlite3.Row]:
        return self.query(
            "SELECT day, COUNT(*) AS pages, "
            "SUM(outcome = 'blocked') AS blocked "
            "FROM fetch_log WHERE outcome != 'cached' "
            "GROUP BY day ORDER BY day DESC LIMIT ?", (days,)
        )

    def get_seller_state(self, seller: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM seller_state WHERE seller = ?", (seller,)
        ).fetchone()

    def mark_seller_attempt(self, seller: str, *, pages: int, new_rows: int,
                            status: str, backfilled: bool | None = None) -> None:
        now = dt.datetime.now().isoformat(timespec="seconds")
        existing = self.get_seller_state(seller)
        blocks = (existing["consecutive_blocks"] if existing else 0)
        blocks = blocks + 1 if status == "blocked" else 0
        done = existing["backfilled"] if existing else 0
        if backfilled is not None:
            done = int(backfilled)
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO seller_state
                       (seller, last_attempt_at, last_success_at, last_pages,
                        last_new_rows, total_rows, backfilled, consecutive_blocks)
                   VALUES (?, ?, ?, ?, ?,
                           (SELECT COUNT(*) FROM listings WHERE seller = ?), ?, ?)
                   ON CONFLICT(seller) DO UPDATE SET
                       last_attempt_at = excluded.last_attempt_at,
                       last_success_at = COALESCE(excluded.last_success_at,
                                                  seller_state.last_success_at),
                       last_pages = excluded.last_pages,
                       last_new_rows = excluded.last_new_rows,
                       total_rows = excluded.total_rows,
                       backfilled = excluded.backfilled,
                       consecutive_blocks = excluded.consecutive_blocks""",
                (seller, now, now if status == "ok" else None, pages, new_rows,
                 seller, done, blocks),
            )

    def sellers_by_staleness(self, users: list[str]) -> list[str]:
        """Least-recently-visited first, never-visited before that.

        This is the rotation: a run touches a handful of sellers, and over a
        week the whole panel comes round without any single day looking busy.
        """
        rows = {
            r["seller"]: r["last_attempt_at"]
            for r in self.query("SELECT seller, last_attempt_at FROM seller_state")
        }
        return sorted(users, key=lambda u: (rows.get(u) is not None, rows.get(u) or ""))

    def known_row_count(self, listings: Iterable[Listing]) -> int:
        """How many of these sales are already stored."""
        count = 0
        for listing in listings:
            hit = self.conn.execute(
                "SELECT 1 FROM listings WHERE row_key = ?", (row_key(listing),)
            ).fetchone()
            if hit:
                count += 1
        return count

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
