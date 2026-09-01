"""Render the analysis as a standalone HTML page and as CSVs."""
from __future__ import annotations

import csv
import datetime as dt
import html
import json
from pathlib import Path
from typing import Any, Sequence

from .analyze import Report

CSS = """
:root {
  --bg:#f7f7f5; --panel:#fff; --ink:#1a1a19; --muted:#6b6b66; --line:#e4e4e0;
  --accent:#b4530a; --accent-soft:#f0d9c6; --up:#1f7a4d; --down:#a12d2d;
}
@media (prefers-color-scheme: dark) {
  :root { --bg:#161614; --panel:#201e1b; --ink:#efece7; --muted:#a09a91;
          --line:#332f2b; --accent:#e08a3c; --accent-soft:#3a2a1a;
          --up:#5fc78f; --down:#e07070; }
}
* { box-sizing:border-box; }
body { margin:0; padding:32px 24px 80px; background:var(--bg); color:var(--ink);
  font:15px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }
.wrap { max-width:1180px; margin:0 auto; }
h1 { font-size:26px; margin:0 0 4px; letter-spacing:-0.02em; }
h2 { font-size:17px; margin:38px 0 12px; letter-spacing:-0.01em; }
h2 small { font-weight:400; color:var(--muted); font-size:13px; margin-left:8px; }
.sub { color:var(--muted); font-size:13px; margin-bottom:24px; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }
.card .label { color:var(--muted); font-size:11px; text-transform:uppercase;
  letter-spacing:.06em; margin-bottom:6px; }
.card .value { font-size:22px; font-weight:600; letter-spacing:-0.02em; }
.panel { background:var(--panel); border:1px solid var(--line); border-radius:10px;
  overflow-x:auto; }
table { border-collapse:collapse; width:100%; font-size:13.5px; }
th, td { padding:8px 12px; text-align:right; white-space:nowrap; border-bottom:1px solid var(--line); }
th { font-size:11px; text-transform:uppercase; letter-spacing:.05em; color:var(--muted);
  font-weight:600; position:sticky; top:0; background:var(--panel); }
td:first-child, th:first-child { text-align:left; white-space:normal; min-width:190px; }
tbody tr:last-child td { border-bottom:none; }
tbody tr:hover { background:var(--accent-soft); }
.bar { display:block; height:6px; border-radius:3px; background:var(--accent); margin-top:5px; }
.up { color:var(--up); } .down { color:var(--down); } .flat { color:var(--muted); }
.note { color:var(--muted); font-size:12.5px; margin:8px 0 0; }
ul.titles { columns:2; font-size:12.5px; color:var(--muted); padding-left:18px; }
@media (max-width:720px){ ul.titles{columns:1;} body{padding:20px 12px 60px;} }
"""


def _money(value: Any) -> str:
    if value is None:
        return "—"
    return f"${value:,.2f}" if abs(value) < 10000 else f"${value:,.0f}"


def _num(value: Any) -> str:
    return "—" if value is None else f"{value:,}"


def _pct_cell(value: Any) -> str:
    if value is None:
        return '<span class="flat">new</span>'
    cls = "up" if value > 0 else ("down" if value < 0 else "flat")
    sign = "+" if value > 0 else ""
    return f'<span class="{cls}">{sign}{value:g}%</span>'


def _table(rows: Sequence[dict], columns: Sequence[tuple[str, str, Any]],
           bar_key: str | None = None) -> str:
    if not rows:
        return '<p class="note">No data in this window.</p>'
    peak = max((r.get(bar_key) or 0) for r in rows) if bar_key else 0
    head = "".join(f"<th>{html.escape(label)}</th>" for label, _, _ in columns)
    body = []
    for row in rows:
        cells = []
        for i, (_, key, fmt) in enumerate(columns):
            value = fmt(row.get(key)) if fmt else html.escape(str(row.get(key, "—")))
            if i == 0 and bar_key and peak:
                width = 100 * (row.get(bar_key) or 0) / peak
                value += f'<span class="bar" style="width:{width:.1f}%"></span>'
            cells.append(f"<td>{value}</td>")
        body.append(f"<tr>{''.join(cells)}</tr>")
    return (f'<div class="panel"><table><thead><tr>{head}</tr></thead>'
            f"<tbody>{''.join(body)}</tbody></table></div>")


_RANK_COLUMNS = [
    ("", "key", None),
    ("Units", "units", _num),
    ("Listings", "listings", _num),
    ("Revenue", "revenue", _money),
    ("Median", "median_price", _money),
    ("P25", "p25_price", _money),
    ("P75", "p75_price", _money),
    ("Avg ship", "avg_shipping", _money),
    ("Free ship", "free_shipping_pct", lambda v: "—" if v is None else f"{v:g}%"),
]

_MOMENTUM_COLUMNS = [
    ("", "key", None),
    ("Last 30d", "recent_units", _num),
    ("Prior 30d", "prior_units", _num),
    ("Δ units", "delta_units", lambda v: f"{v:+,}" if v is not None else "—"),
    ("Change", "change_pct", _pct_cell),
    ("Median now", "recent_median", _money),
    ("Median before", "prior_median", _money),
    ("Revenue 30d", "recent_revenue", _money),
]


def render_html(report: Report, *, title: str = "Used Car Parts — Sold Market Report") -> str:
    totals, cov = report.totals, report.coverage
    generated = report.generated_at.strftime("%Y-%m-%d %H:%M")

    cards = [
        ("Units sold", _num(totals["units"])),
        ("Listings", _num(totals["listings"])),
        ("Revenue", _money(totals["revenue"])),
        ("With shipping", _money(totals["gross_with_shipping"])),
        ("Median price", _money(totals["median_price"])),
        ("Average price", _money(totals["avg_price"])),
        ("Avg shipping", _money(totals["avg_shipping"])),
        ("Free shipping", f"{totals['free_shipping_pct']:g}%"
            if totals["free_shipping_pct"] is not None else "—"),
    ]
    cards_html = "".join(
        f'<div class="card"><div class="label">{html.escape(label)}</div>'
        f'<div class="value">{value}</div></div>'
        for label, value in cards
    )

    sections = [
        ("Hottest part types", "momentum over the last 30 days vs the 30 before",
         _table(report.hot_categories, _MOMENTUM_COLUMNS, bar_key="recent_units")),
        ("Hottest vehicles", "momentum over the last 30 days vs the 30 before",
         _table(report.hot_models, _MOMENTUM_COLUMNS, bar_key="recent_units")),
        ("Part types by volume", "which piece sells most",
         _table(report.categories, _RANK_COLUMNS, bar_key="units")),
        ("Part groups", "coarse rollup",
         _table(report.groups, _RANK_COLUMNS, bar_key="units")),
        ("Car brands", "which make is hot",
         _table(report.makes, _RANK_COLUMNS, bar_key="units")),
        ("Models", "make + model",
         _table(report.models, _RANK_COLUMNS, bar_key="units")),
        ("Model generations", "make + model + year span",
         _table(report.generations, _RANK_COLUMNS, bar_key="units")),
        ("Vehicle × part", "the actionable pairing — what to buy for what car",
         _table(report.combos, _RANK_COLUMNS, bar_key="units")),
        ("Model year", "which years the demand sits on",
         _table(report.model_years, _RANK_COLUMNS, bar_key="units")),
        ("Condition", "",
         _table(report.conditions, _RANK_COLUMNS, bar_key="units")),
        ("Weekly volume", "sales per week across the window",
         _table(report.trend, _RANK_COLUMNS, bar_key="units")),
        ("Sellers in the panel", "coverage check, not a ranking of who is better",
         _table(report.sellers, _RANK_COLUMNS, bar_key="units")),
    ]
    body = "".join(
        f"<h2>{html.escape(name)}{f'<small>{html.escape(note)}</small>' if note else ''}</h2>{table}"
        for name, note, table in sections
    )

    if report.uncategorized:
        items = "".join(f"<li>{html.escape(t)}</li>" for t in report.uncategorized)
        body += (
            "<h2>Titles the taxonomy missed<small>add keywords to config/categories.yml, "
            "then run <code>reindex</code></small></h2>"
            f'<ul class="titles">{items}</ul>'
        )

    coverage_line = (
        f"Taxonomy coverage — make {cov['with_make_pct']}% · model {cov['with_model_pct']}% · "
        f"part type {cov['with_category_pct']}% · sold date {cov['with_date_pct']}% · "
        f"shipping {cov['with_shipping_pct']}%"
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{CSS}</style></head>
<body><div class="wrap">
<h1>{html.escape(title)}</h1>
<p class="sub">Last {report.window_days} days · generated {generated}<br>{coverage_line}</p>
<div class="cards">{cards_html}</div>
{body}
</div></body></html>"""


def write_html(report: Report, path: Path | str, **kwargs) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_html(report, **kwargs), encoding="utf-8")
    return path


def write_csvs(report: Report, directory: Path | str) -> list[Path]:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    tables = {
        "categories": report.categories,
        "part_groups": report.groups,
        "makes": report.makes,
        "models": report.models,
        "generations": report.generations,
        "vehicle_x_part": report.combos,
        "model_years": report.model_years,
        "conditions": report.conditions,
        "sellers": report.sellers,
        "weekly_trend": report.trend,
        "hot_categories": report.hot_categories,
        "hot_models": report.hot_models,
    }
    written = []
    for name, rows in tables.items():
        if not rows:
            continue
        path = directory / f"{name}.csv"
        # utf-8-sig: Excel on Windows assumes cp1252 for a BOM-less UTF-8 file
        # and renders "Vehicle x part" keys as mojibake.
        with path.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        written.append(path)
    return written


def write_json(report: Report, path: Path | str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        k: v for k, v in report.__dict__.items() if k != "generated_at"
    }
    payload["generated_at"] = report.generated_at.isoformat(timespec="seconds")
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def export_listings(store, path: Path | str, *, days: int | None = 90,
                    today: dt.date | None = None) -> Path:
    """Dump the raw rows -- title, price, shipping, date -- for spreadsheet work."""
    from .analyze import SELECT_COLUMNS  # noqa: F401  (documents the column set)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    sql = ("SELECT item_id, sold_date, title, make, model, year_from, year_to, "
           "part_category, part_group, condition, price, currency, original_price, "
           "shipping_cost, shipping_status, total_price, quantity_sold, is_oem, "
           "side, position, seller, url FROM listings")
    params: tuple = ()
    if days:
        cutoff = ((today or dt.date.today()) - dt.timedelta(days=days)).isoformat()
        sql += " WHERE sold_date >= ?"
        params = (cutoff,)
    sql += " ORDER BY sold_date DESC, item_id"

    rows = store.query(sql, params)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        if rows:
            writer.writerow(rows[0].keys())
            writer.writerows([tuple(r) for r in rows])
    return path
