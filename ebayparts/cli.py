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
from .inbox import find_pages, import_folder
from .probe import DEFAULT_PROFILES, available_profiles, diagnose, run_probe
from .report import export_listings, write_csvs, write_html, write_json
from .scrape import DayBudget, discover_sellers, scrape_all
from .urls import sold_search_url
from .store import Store

log = logging.getLogger("ebayparts")


# --------------------------------------------------------------- utilities

def configure_console() -> None:
    """Force UTF-8 on stdout/stderr.

    Windows consoles still default to a legacy code page (cp1252), and this tool
    prints em dashes and middots. Without this, `top` and `stats` die with
    UnicodeEncodeError -- and worse, so does a scrape that is hours in.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass  # redirected to a pipe that cannot be reconfigured; harmless


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

    mode = args.mode or settings.mode
    pacing = settings.pacing(mode)
    print(f"Mode {mode}: up to {pacing.sellers_per_run} seller(s) this run, "
          f"{pacing.daily_page_budget} pages/day, "
          f"{pacing.delay_seconds:.0f}-{pacing.delay_seconds + pacing.delay_jitter:.0f}s "
          f"between pages.")
    with open_store(settings) as store:
        results = scrape_all(
            sellers, settings, store, mode=mode,
            max_pages=args.max_pages, since=since,
            use_cache=not args.no_cache, engine=args.engine,
            ignore_hours=args.ignore_hours,
        )
        if not results:
            print("Nothing to do this run -- see the message above.")
            return 0
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
        budget = DayBudget(store, pacing.daily_page_budget)
        print(f"Pages fetched today: {budget.used}/{budget.limit}.")
        blocked = [r for r in results if r.status == "blocked"]
        if blocked:
            print(f"\neBay refused a request. The run stopped rather than pushing. "
                  f"Leave it until tomorrow; if it repeats, raise delay_seconds and "
                  f"lower sellers_per_run in config/settings.yml.", file=sys.stderr)
    return 0


COVERAGE_COLUMNS = [
    ("seller", "seller"), ("last collected", "last_success"),
    ("days ago", "days_since"), ("rows", "rows"), ("status", "status"),
]


def print_coverage_warnings(report: list[dict], lookback_days: int) -> None:
    """Surface sellers drifting toward the edge of eBay's 90-day window."""
    # A seller blocked run after run is never collected, so it stays "pending"
    # forever and would otherwise slip past a staleness-only filter.
    problems = [r for r in report
                if r["status"] in ("stale", "at risk", "gap") or r["blocks"] >= 3]
    if not problems:
        return

    print("\nCoverage warnings:")
    print_table(problems, COVERAGE_COLUMNS, limit=len(problems))

    gaps = [r for r in problems if r["status"] == "gap"]
    at_risk = [r for r in problems if r["status"] == "at risk"]
    if gaps:
        print(f"\n  {len(gaps)} seller(s) have not been collected in "
              f"{lookback_days}+ days. eBay has already dropped the sales from "
              f"that gap -- they cannot be recovered by scraping harder now. "
              f"Run again to resume from today.")
    if at_risk:
        print(f"\n  {len(at_risk)} seller(s) are close to the {lookback_days}-day "
              f"edge. Run soon, or raise sellers_per_run so the rotation comes "
              f"round faster.")
    blocked = [r for r in problems if r["blocks"] >= 3]
    if blocked:
        print(f"\n  {len(blocked)} seller(s) have been blocked "
              f"{max(r['blocks'] for r in blocked)}+ runs in a row -- they are "
              f"being attempted but never collected.")


def cmd_plan(args, settings: Settings) -> int:
    """Show what the next run would do. Touches nothing on the network."""
    mode = args.mode or settings.mode
    pacing = settings.pacing(mode)
    sellers = [s for s in load_sellers() if s.enabled]
    now = dt.datetime.now()

    with open_store(settings) as store:
        budget = DayBudget(store, pacing.daily_page_budget, now.date())
        order = store.sellers_by_staleness([s.user for s in sellers])
        queue = order[: pacing.sellers_per_run]
        states = {u: store.get_seller_state(u) for u in order}
        history = store.fetch_history(days=7)
        coverage_rows = store.coverage_report(
            [s.user for s in sellers], lookback_days=settings.lookback_days)

    start, end = pacing.active_hours
    ok_hours = pacing.within_active_hours(now)

    print(f"\nMode           : {mode}")
    print(f"Panel          : {len(sellers)} seller(s), {pacing.sellers_per_run} per run "
          f"-> full sweep every {-(-len(sellers) // max(1, pacing.sellers_per_run))} run(s)")
    print(f"Pages          : up to {pacing.pages_per_seller}/seller, "
          f"stops after {pacing.pages_without_new} page(s) with nothing new")
    print(f"Gap            : {pacing.delay_seconds:.0f}-"
          f"{pacing.delay_seconds + pacing.delay_jitter:.0f}s, plus a "
          f"{pacing.long_pause_seconds:.0f}s break every {pacing.long_pause_every} pages")
    print(f"Active hours   : {start:02d}:00-{end:02d}:00 "
          f"({'now inside' if ok_hours else 'NOW OUTSIDE -- run would decline'})")
    print(f"Budget today   : {budget.used}/{budget.limit} used, {budget.remaining} left")

    worst = pacing.sellers_per_run * pacing.pages_per_seller
    worst = min(worst, budget.remaining)
    est_min = worst * (pacing.delay_seconds + pacing.delay_jitter / 2) / 60
    print(f"This run       : at most {worst} page(s), roughly {est_min:.0f} min")

    print("\nNext up (least recently visited first):")
    rows = []
    for user in queue:
        state = states.get(user)
        rows.append({
            "seller": user,
            "last visit": (state["last_attempt_at"][:16].replace("T", " ")
                           if state and state["last_attempt_at"] else "never"),
            "rows": state["total_rows"] if state else 0,
            "new last time": state["last_new_rows"] if state else "-",
        })
    print_table(rows, [("seller", "seller"), ("last visit", "last visit"),
                       ("rows held", "rows"), ("new last time", "new last time")],
                limit=len(rows))

    if history:
        print("\nPages fetched, recent days:")
        print_table([dict(r) for r in history],
                    [("day", "day"), ("pages", "pages"), ("blocked", "blocked")],
                    limit=7)

    print_coverage_warnings(coverage_rows, settings.lookback_days)
    return 0


def cmd_browser(args, settings: Settings) -> int:
    """Open the persistent browser profile for a one-off manual visit.

    Do this once before the first browser run: accept the cookie banner, set
    your region, and let the profile look like one that has been used. Sign in
    only if you accept the account risk -- an IP block is recoverable, a flagged
    selling account is a bigger problem.
    """
    from .fetch import Fetcher

    print(f"Opening {settings.marketplace} in the profile at "
          f"{settings.resolve('browser_profile_dir')}.")
    print("Accept any cookie banner, then close the window when you are done.\n")
    settings.browser_headless = False
    fetcher = Fetcher(settings, use_cache=False, engine="playwright")
    try:
        fetcher.open_browser(args.url)
        input("Press Enter here once you have finished in the browser... ")
    finally:
        fetcher.close()
    print("Profile saved. Now run:  python -m ebayparts scrape --engine playwright")
    return 0


def cmd_urls(args, settings: Settings) -> int:
    """Print the sold-search URL for each seller, to open in your own browser."""
    sellers = [s for s in load_sellers() if s.enabled]
    if not sellers:
        print("No sellers configured.", file=sys.stderr)
        return 1
    if args.sellers:
        wanted = {u.strip().lower() for u in args.sellers.split(",")}
        sellers = [s for s in sellers if s.user.lower() in wanted]

    urls = []
    for seller in sellers:
        for page in range(1, args.pages + 1):
            urls.append((seller.user, page, sold_search_url(settings, seller, page=page)))

    if args.open:
        import webbrowser
        print(f"Opening {len(urls)} tab(s)...")
        for _, _, url in urls:
            webbrowser.open_new_tab(url)
        print("\nIn each tab: Ctrl+S -> save as \"Webpage, HTML Only\" into")
        print(f"  {settings.resolve('inbox_dir')}")
        print("\nThen run:  python -m ebayparts import")
        return 0

    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        links = "\n".join(
            f'<li><a href="{u}" target="_blank">{s} — page {p}</a></li>'
            for s, p, u in urls
        )
        path.write_text(
            f"<!doctype html><meta charset=utf-8><title>eBay sold pages</title>"
            f"<h1>{len(urls)} page(s) to save</h1><ol>{links}</ol>",
            encoding="utf-8",
        )
        print(f"Wrote {path} — open it and middle-click each link.")
        return 0

    for _, _, url in urls:
        print(url)
    return 0


def cmd_import(args, settings: Settings) -> int:
    """Parse sold pages you saved from your browser."""
    folder = Path(args.folder) if args.folder else settings.resolve("inbox_dir")
    folder.mkdir(parents=True, exist_ok=True)

    pages = find_pages(folder)
    if not pages:
        print(f"No saved pages found in {folder}\n")
        print("Save eBay sold-search pages there first:")
        print("  1. python -m ebayparts urls --open")
        print("  2. in each tab: Ctrl+S, choose \"Webpage, HTML Only\"")
        print(f"  3. save into {folder}")
        print("  4. run this command again")
        return 1

    print(f"Importing {len(pages)} page(s) from {folder}\n")
    with open_store(settings) as store:
        results = import_folder(folder, store, archive=args.archive)
        rows = [{
            "file": r.path.name[:44],
            "listings": r.listings,
            "new": r.new,
            "seller": ", ".join(r.sellers)[:22] or "-",
            "status": r.status if r.status != "ok" else "",
        } for r in results]
        print_table(rows, [("file", "file"), ("listings", "listings"), ("new", "new"),
                           ("seller", "seller"), ("note", "status")],
                    limit=len(rows))
        total_new = sum(r.new for r in results)
        print(f"\nStored {total_new:,} new sale(s). Database now holds "
              f"{store.count():,} rows ({' to '.join(str(d) for d in store.date_span())}).")

    problems = [r for r in results if r.status != "ok"]
    if problems:
        print(f"\n{len(problems)} file(s) had trouble:")
        for r in problems:
            print(f"  {r.path.name}: {r.note}")
    if total_new:
        print("\nNext:  python -m ebayparts report")
    return 0


def cmd_probe(args, settings: Settings) -> int:
    """Find a browser profile eBay answers on this connection."""
    if args.list:
        profiles = available_profiles()
        if not profiles:
            print("curl_cffi is not installed.", file=sys.stderr)
            return 1
        print(f"\n{len(profiles)} impersonation targets available:\n")
        for i in range(0, len(profiles), 6):
            print("  " + "  ".join(f"{p:<18}" for p in profiles[i:i + 6]))
        return 0

    chosen = args.profiles.split(",") if args.profiles else DEFAULT_PROFILES
    seller = args.seller or (load_sellers()[0].user if load_sellers() else None)
    print(f"\nProbing eBay with {len(chosen)} profile(s), "
          f"~{args.delay:.0f}s apart. Seller: {seller or 'category search'}.")
    print("This makes a handful of requests, once. Ctrl-C to stop.\n")

    results = run_probe(settings, profiles=chosen, seller=seller,
                        delay=args.delay, try_warm_up=not args.no_warm_up)

    rows = [{
        "profile": r.profile,
        "warm": "yes" if r.warm_up else "no",
        "http": r.http if r.http is not None else "-",
        "result": r.status,
        "listings": r.listings,
        "detail": r.title or r.detail,
    } for r in results]
    print_table(rows, [("profile", "profile"), ("warm-up", "warm"), ("http", "http"),
                       ("result", "result"), ("items", "listings"),
                       ("page / error", "detail")], limit=len(rows))

    winner = next((r for r in results if r.works), None)
    if winner:
        print(f"\n  {winner.profile} works"
              f"{' with warm_up' if winner.warm_up else ''} "
              f"({winner.listings} listings parsed).\n")
        print("  Put this in config/settings.yml:\n")
        print(f"    impersonate: {winner.profile}")
        print(f"    warm_up: {'true' if winner.warm_up else 'false'}\n")
        return 0

    print("\n  No profile got through.")
    if diagnose(results) == "refused":
        print("  eBay is answering but refusing this connection -- it is the IP or\n"
              "  the account-less session being turned away, not the client config.\n"
              "  Nothing in this tool will change that. Your options:\n"
              "    1. Save a results page from your own browser and parse it:\n"
              "         python -m ebayparts parse-file page.html --debug --save\n"
              "    2. Try --engine playwright (a real browser, harder to refuse)\n"
              "    3. Apply for eBay's Marketplace Insights API, which serves this\n"
              "       same 90-day sold data under terms that permit it")
    else:
        print("  These look like network errors rather than refusals -- check your\n"
              "  connection, VPN, or firewall and try again.")
    return 1


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
        panel = [s.user for s in load_sellers() if s.enabled]
        coverage_rows = store.coverage_report(
            panel, lookback_days=settings.lookback_days)
        budget_used = store.pages_fetched_today()

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
    pacing = settings.pacing()
    print(f"Today    : {budget_used}/{pacing.daily_page_budget} pages fetched")

    if coverage_rows:
        counts: dict[str, int] = {}
        for row in coverage_rows:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        print("Coverage : " + " · ".join(f"{v} {k}" for k, v in counts.items()))

    if runs:
        print("\nRecent runs:")
        print_table([dict(r) for r in runs],
                    [("seller", "seller"), ("finished", "finished_at"), ("pages", "pages"),
                     ("rows", "rows_found"), ("new", "rows_new"), ("status", "status")],
                    limit=10)

    print_coverage_warnings(coverage_rows, settings.lookback_days)
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
    p.add_argument("--mode", choices=["passive", "backfill"],
                   help="pacing profile (default from settings.yml)")
    p.add_argument("--ignore-hours", action="store_true",
                   help="run even outside the configured active hours")
    p.set_defaults(func=cmd_scrape)

    p = sub.add_parser("browser", help="open the persistent browser profile once, by hand")
    p.add_argument("--url", help="page to open (default: the eBay homepage)")
    p.set_defaults(func=cmd_browser)

    p = sub.add_parser("urls", help="print/open the sold pages to save from your browser")
    p.add_argument("--pages", type=int, default=1, help="pages per seller (default 1)")
    p.add_argument("--sellers", help="comma-separated usernames")
    p.add_argument("--open", action="store_true", help="open them as browser tabs")
    p.add_argument("--out", help="write a clickable HTML index instead")
    p.set_defaults(func=cmd_urls)

    p = sub.add_parser("import", help="parse sold pages you saved from your browser")
    p.add_argument("--folder", help="where the saved pages are (default data/inbox)")
    p.add_argument("--archive", action="store_true",
                   help="move imported files into an imported/ subfolder")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("probe", help="find a browser profile eBay answers on this connection")
    p.add_argument("--profiles", help="comma-separated targets to try")
    p.add_argument("--seller", help="seller to test against (default: first configured)")
    p.add_argument("--delay", type=float, default=6.0, help="seconds between attempts")
    p.add_argument("--no-warm-up", action="store_true",
                   help="skip the homepage visit variant")
    p.add_argument("--list", action="store_true", help="list valid targets and exit")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("plan", help="show what the next run would do; touches nothing")
    p.add_argument("--mode", choices=["passive", "backfill"])
    p.set_defaults(func=cmd_plan)

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
    configure_console()
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
