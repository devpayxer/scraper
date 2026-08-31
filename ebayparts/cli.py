"""Command line interface: python -m ebayparts <command>."""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from pathlib import Path

from . import __version__
from .analyze import build_report, coverage, group_by, load_rows, momentum, price_stats
from .config import ROOT, Settings, load_sellers
from .report import export_listings, write_csvs, write_html, write_json
from .scrape import discover_sellers, scrape_all
from .store import Store

log = logging.getLogger("ebayparts")


# --------------------------------------------------------------- utilities

def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(message)s",
        datefmt="%H:%M:%S",
    )


def print_table(rows: list[dict], columns: list[tuple[str, str]], limit: int = 25) -> None:
    """Minimal fixed-width table for the terminal."""
    if not rows:
        print("  (no data)")
        return
    rows = rows[:limit]

    def fmt(value) -> str:
        if value is None:
            return "-"
        if isinstance(value, float):
            return f"{value:,.2f}"
        if isinstance(value, int):
            return f"{value:,}"
        return str(value)

    widths = []
    for label, key in columns:
        widths.append(max(len(label), *(len(fmt(r.get(key))) for r in rows)))
    header = "  ".join(label.ljust(w) if i == 0 else label.rjust(w)
                       for i, ((label, _), w) in enumerate(zip(columns, widths)))
    print("  " + header)
    print("  " + "-" * len(header))
    for row in rows:
        line = "  ".join(
            fmt(row.get(key)).ljust(w) if i == 0 else fmt(row.get(key)).rjust(w)
            for i, ((_, key), w) in enumerate(zip(columns, widths))
        )
        print("  " + line)


RANK_COLUMNS = [
    ("", "key"), ("units", "units"), ("listings", "listings"), ("revenue", "revenue"),
    ("median", "median_price"), ("p25", "p25_price"), ("p75", "p75_price"),
    ("ship", "avg_shipping"),
]
MOMENTUM_COLUMNS = [
    ("", "key"), ("30d", "recent_units"), ("prior", "prior_units"),
    ("delta", "delta_units"), ("change%", "change_pct"), ("median", "recent_median"),
]


def open_store(settings: Settings) -> Store:
    return Store(settings.resolve("database"))


# --------------------------------------------------------------- commands

def cmd_scrape(args, settings: Settings) -> int:
    sellers = load_sellers(only=args.sellers.split(",") if args.sellers else None)
    if not sellers:
        print("No sellers configured. Add them to config/sellers.yml "
              "(or find some with: python -m ebayparts discover).", file=sys.stderr)
        return 1

    since = dt.date.fromisoformat(args.since) if args.since else None
    if since is None and args.days:
        since = dt.date.today() - dt.timedelta(days=args.days)

    print(f"Scraping {len(sellers)} seller(s) via {args.engine or settings.engine}; "
          f"~{settings.delay_seconds:.0f}-{settings.delay_seconds + settings.delay_jitter:.0f}s "
          f"between pages.")
    with open_store(settings) as store:
        results = scrape_all(
            sellers, settings, store,
            max_pages=args.max_pages, since=since,
            use_cache=not args.no_cache, engine=args.engine,
        )
        print("\nDone.")
        print_table(
            [{"key": r.seller, "pages": r.pages, "rows": r.rows_found,
              "new": r.rows_new, "status": r.status} for r in results],
            [("seller", "key"), ("pages", "pages"), ("rows", "rows"),
             ("new", "new"), ("status", "status")],
            limit=len(results),
        )
        print(f"\nDatabase now holds {store.count():,} sold rows "
              f"({' to '.join(str(d) for d in store.date_span())}).")
        blocked = [r for r in results if r.status == "blocked"]
        if blocked:
            print(f"\n{len(blocked)} seller(s) hit a block. Slow down "
                  f"(raise delay_seconds in config/settings.yml) or try "
                  f"--engine playwright.", file=sys.stderr)
    return 0


def cmd_discover(args, settings: Settings) -> int:
    ranked = discover_sellers(
        settings, category=args.category, pages=args.pages,
        use_cache=not args.no_cache, engine=args.engine,
    )
    if not ranked:
        print("No sellers found -- eBay likely blocked the request.", file=sys.stderr)
        return 1
    print(f"\n{len(ranked)} sellers seen across {args.pages} page(s):\n")
    print_table([{"key": name, "listings": n} for name, n in ranked],
                [("seller", "key"), ("sold listings seen", "listings")],
                limit=args.limit)
    print("\nPaste the ones you want into config/sellers.yml:\n")
    for name, _ in ranked[:args.limit]:
        print(f"  - user: {name}")
    return 0


def cmd_parse_file(args, settings: Settings) -> int:
    """Parse a saved results page. The way to validate the parser offline.

    Save the page from your browser (Ctrl+S, "Web page, HTML only") and run:
        python -m ebayparts parse-file page.html --debug
    """
    from .parse import parse_search_page, result_count

    path = Path(args.path)
    html = path.read_text(encoding="utf-8", errors="replace")
    listings = parse_search_page(html, source_url=str(path))

    total = result_count(html)
    print(f"{path.name}: {len(listings)} listing(s) parsed"
          + (f"; page header claims {total:,} results" if total else ""))

    if not listings:
        print("\nNothing matched. The page is probably a block/captcha page, or eBay "
              "changed its markup -- check the first 400 characters:\n")
        print("  " + html[:400].replace("\n", " "))
        return 1

    if args.debug:
        for item in listings[: args.limit]:
            print(f"\n  {item.item_id}  {item.sold_date}  {item.price} {item.currency} "
                  f"+{item.shipping_cost} ({item.shipping_status}) = {item.total_price}")
            print(f"    {item.title}")
            print(f"    {item.make} / {item.model} / {item.year_from}-{item.year_to} "
                  f"| {item.part_category} | {item.condition} | {item.seller}")
        missing = {
            "sold_date": sum(1 for i in listings if i.sold_date is None),
            "price": sum(1 for i in listings if i.price is None),
            "shipping": sum(1 for i in listings if i.shipping_cost is None),
            "make": sum(1 for i in listings if i.make is None),
            "part_category": sum(1 for i in listings if i.part_category is None),
        }
        print("\n  missing fields:", ", ".join(f"{k}={v}" for k, v in missing.items()))

    if args.save:
        with open_store(settings) as store:
            found, new = store.upsert_many(listings)
            print(f"\nStored {found} row(s), {new} new. Total {store.count():,}.")
    return 0


def cmd_reindex(args, settings: Settings) -> int:
    with open_store(settings) as store:
        count = store.reindex()
    print(f"Re-derived make/model/part type for {count:,} rows from the stored titles.")
    return 0


def cmd_report(args, settings: Settings) -> int:
    with open_store(settings) as store:
        rows = load_rows(store, days=args.days)
        if not rows:
            print("No priced rows in the window. Run `scrape` first.", file=sys.stderr)
            return 1
        report = build_report(rows, window_days=args.days)

    out_dir = Path(args.out) if args.out else ROOT / "out"
    html_path = write_html(report, out_dir / "report.html")
    print(f"Wrote {html_path}")
    if args.csv:
        for path in write_csvs(report, out_dir / "csv"):
            print(f"Wrote {path}")
    if args.json:
        print(f"Wrote {write_json(report, out_dir / 'report.json')}")
    return 0


def cmd_export(args, settings: Settings) -> int:
    with open_store(settings) as store:
        path = export_listings(store, args.out or (ROOT / "out" / "listings.csv"),
                               days=args.days)
    print(f"Wrote {path}")
    return 0


def cmd_top(args, settings: Settings) -> int:
    with open_store(settings) as store:
        rows = load_rows(store, days=args.days)
    if not rows:
        print("No data yet. Run `scrape` first.", file=sys.stderr)
        return 1

    # Narrow breakdowns need a couple of sales before a median means anything;
    # broad ones do not.
    default_min = 2 if args.by in ("model", "generation", "combo") else 1
    minimum = args.min if args.min is not None else default_min

    keys = {
        "category": lambda r: r.part_category,
        "group": lambda r: r.part_group,
        "make": lambda r: r.make,
        "model": lambda r: r.vehicle,
        "generation": lambda r: r.generation,
        "combo": lambda r: (f"{r.vehicle} — {r.part_category}"
                            if r.make and r.part_category else None),
        "year": lambda r: r.year_from,
        "seller": lambda r: r.seller,
    }
    ranked = group_by(rows, keys[args.by], limit=args.limit, min_listings=minimum)

    print(f"\nTop {args.by} — last {args.days} days ({len(rows):,} sales)\n")
    print_table(ranked, RANK_COLUMNS, limit=args.limit)
    if not ranked and minimum > 1:
        print(f"\n  Nothing has {minimum}+ sales yet at this breakdown. "
              f"Retry with --min 1, or scrape more sellers.")
    return 0


def cmd_hot(args, settings: Settings) -> int:
    with open_store(settings) as store:
        rows = load_rows(store, days=args.days)
    if not rows:
        print("No data yet. Run `scrape` first.", file=sys.stderr)
        return 1
    keys = {
        "category": lambda r: r.part_category,
        "make": lambda r: r.make,
        "model": lambda r: r.vehicle,
        "generation": lambda r: r.generation,
    }
    print(f"\nMomentum by {args.by} — last {args.window}d vs the {args.window}d before\n")
    print_table(momentum(rows, keys[args.by], recent_days=args.window, limit=args.limit),
                MOMENTUM_COLUMNS, limit=args.limit)
    return 0


def cmd_stats(args, settings: Settings) -> int:
    with open_store(settings) as store:
        total = store.count()
        span = store.date_span()
        sellers = store.sellers()
        rows = load_rows(store, days=args.days)
        runs = store.query(
            "SELECT seller, finished_at, pages, rows_found, rows_new, status "
            "FROM scrape_runs ORDER BY id DESC LIMIT 10"
        )

    print(f"\nDatabase : {settings.resolve('database')}")
    print(f"Rows     : {total:,}  ({span[0] or '-'} to {span[1] or '-'})")
    print(f"Sellers  : {len(sellers)}")
    if rows:
        stats = price_stats(rows)
        cov = coverage(rows)
        print(f"\nLast {args.days} days: {stats['units']:,} units, "
              f"${stats['revenue']:,.2f} revenue, median ${stats['median_price']:,.2f}")
        print(f"Recognised: make {cov['with_make_pct']}% · model {cov['with_model_pct']}% "
              f"· part type {cov['with_category_pct']}% · date {cov['with_date_pct']}%")
    if runs:
        print("\nRecent runs:")
        print_table([dict(r) for r in runs],
                    [("seller", "seller"), ("finished", "finished_at"), ("pages", "pages"),
                     ("rows", "rows_found"), ("new", "rows_new"), ("status", "status")],
                    limit=10)
    return 0


def cmd_sellers(args, settings: Settings) -> int:
    sellers = load_sellers()
    print(f"\n{len(sellers)} configured seller(s):\n")
    print_table(
        [{"user": s.user, "label": s.name, "enabled": "yes" if s.enabled else "no"}
         for s in sellers],
        [("user", "user"), ("label", "label"), ("enabled", "enabled")],
        limit=len(sellers),
    )
    return 0


# ------------------------------------------------------------------ parser

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ebayparts",
        description="Collect eBay sold used-car-part listings and analyse what sells.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--config", help="path to settings.yml")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scrape", help="collect sold listings for the configured sellers")
    p.add_argument("--sellers", help="comma-separated usernames; default is all enabled")
    p.add_argument("--max-pages", type=int, help="cap pages per seller")
    p.add_argument("--days", type=int, help="skip sales older than N days")
    p.add_argument("--since", help="skip sales before this ISO date")
    p.add_argument("--no-cache", action="store_true", help="ignore the page cache")
    p.add_argument("--engine", choices=["curl_cffi", "playwright"])
    p.set_defaults(func=cmd_scrape)

    p = sub.add_parser("discover", help="find high-volume sellers to add to the panel")
    p.add_argument("--category", type=int, help="eBay category id (default from settings)")
    p.add_argument("--pages", type=int, default=10)
    p.add_argument("--limit", type=int, default=60)
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--engine", choices=["curl_cffi", "playwright"])
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("parse-file", help="parse a saved HTML page (offline validation)")
    p.add_argument("path")
    p.add_argument("--debug", action="store_true", help="print every parsed field")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--save", action="store_true", help="store the rows in the database")
    p.set_defaults(func=cmd_parse_file)

    p = sub.add_parser("reindex", help="re-derive make/model/part type after editing configs")
    p.set_defaults(func=cmd_reindex)

    p = sub.add_parser("report", help="write the HTML market report")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--out", help="output directory (default ./out)")
    p.add_argument("--csv", action="store_true", help="also write one CSV per table")
    p.add_argument("--json", action="store_true", help="also write report.json")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("export", help="dump raw sold rows to CSV")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--out")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("top", help="ranking table in the terminal")
    p.add_argument("--by", default="category",
                   choices=["category", "group", "make", "model", "generation",
                            "combo", "year", "seller"])
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--min", type=int,
                   help="minimum sales per bucket (default 2 for model/generation/combo)")
    p.set_defaults(func=cmd_top)

    p = sub.add_parser("hot", help="what is trending up or down")
    p.add_argument("--by", default="category",
                   choices=["category", "make", "model", "generation"])
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--window", type=int, default=30)
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(func=cmd_hot)

    p = sub.add_parser("stats", help="database health and recent runs")
    p.add_argument("--days", type=int, default=90)
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("sellers", help="list the configured seller panel")
    p.set_defaults(func=cmd_sellers)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    settings = Settings.load(Path(args.config) if args.config else None)
    try:
        return args.func(args, settings)
    except KeyboardInterrupt:
        print("\nInterrupted. Everything scraped so far is already saved.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
