# eBay Used Car Parts — Sold Market Scanner

An internal research tool. It collects **sold** listings from a panel of eBay
used-car-part sellers, extracts **title, price, shipping and sale date**, then
tells you what is actually moving:

* which **part type** sells most (headlights vs. mirrors vs. ECUs)
* which **car brand, model and generation** is hot
* what things **sell for** — median, quartiles, shipping
* what is **trending up or down** month over month

It compares *items*, not sellers. Sellers are just the sampling frame.

---

## Read this first

**eBay's User Agreement prohibits scraping**, and eBay actively blocks
automated traffic. Two things follow:

1. **The sanctioned route is the official API.** eBay's
   [Marketplace Insights API](https://developer.ebay.com/api-docs/buy/marketplace-insights/overview.html)
   serves exactly this data — the last 90 days of sold items with prices — under
   terms that permit it. Access is application-gated, but if this study becomes
   something you rely on, apply for it. The
   [Browse API](https://developer.ebay.com/api-docs/buy/browse/overview.html) is
   open but only covers *active* listings, so it cannot answer "what sold".
2. **If you scrape anyway, do it small and slow.** This tool defaults to one
   page every 6–10 seconds, single-threaded, with an aggressive on-disk cache so
   a re-run costs nothing. Those defaults exist for a reason — leave them alone.
   There is no proxy rotation and no CAPTCHA solving here by design; if eBay
   blocks you, the answer is to slow down or use the API, not to hide harder.

Also note: **eBay only publishes ~90 days of sold history**, which happens to be
exactly the window you asked for. There is no way to reach further back through
search — anything longer has to be accumulated by running this on a schedule and
letting the database grow.

---

## Where to run it

**On a normal home PC or laptop, on residential internet. Not on a VPS.**

This is counterintuitive, so it is worth being blunt: eBay blocks datacenter IP
ranges hard, and every cheap VPS (DigitalOcean, Hetzner, AWS Lightsail, Contabo)
lives in exactly those ranges. A VPS buys you uptime and costs you the thing that
makes the tool work. The container this repo was developed in is effectively a
VPS, and eBay 403s it on every request, browser or not.

You do not need a machine that is always on. eBay serves the **whole 90-day sold
history on every request**, so a run that happens weekly captures the same data
as one that happens nightly. Your PC being asleep costs you nothing as long as
you run it at least every couple of months.

## Install

### Windows

```bat
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt

.venv\Scripts\python -m ebayparts scrape --max-pages 2
.venv\Scripts\python -m ebayparts report
start out\report.html
```

Use **Windows Terminal or PowerShell**, not the old `cmd.exe`. (The tool forces
UTF-8 on its own output so it survives `cmd.exe`'s cp437 code page, but the
modern terminal is nicer anyway.)

To run it on a schedule, point Task Scheduler at `scripts\run_windows.bat`:

* **Program/script**: `C:\Users\you\scraper\scripts\run_windows.bat`
* **Start in**: `C:\Users\you\scraper` ← required, or relative paths break
* Tick **"Run task as soon as possible after a scheduled start is missed"** so a
  sleeping PC just catches up on the next boot.

### macOS / Linux

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### Either way

`curl_cffi` matters: it impersonates a real Chrome TLS fingerprint. Plain
`requests`/`urllib` get a flat 403 from eBay.

Optional heavier fallback:

```bash
pip install playwright && playwright install chromium   # then: --engine playwright
```

---

## Quick start

```bash
# 1. what sellers are configured
python -m ebayparts sellers

# 2. collect. Start small to confirm it works end to end.
python -m ebayparts scrape --max-pages 2

# 3. look at it
python -m ebayparts top --by category
python -m ebayparts top --by model
python -m ebayparts hot --by category

# 4. full report
python -m ebayparts report --csv
open out/report.html
```

---

## Building the 50+ seller panel

`config/sellers.yml` ships with the one seller from your screenshot. To find the
rest, let the data pick them — the high-volume recyclers are the ones that keep
reappearing in category-wide sold results:

```bash
python -m ebayparts discover --category 6028 --pages 15
```

It prints a ranked list plus a paste-ready YAML block. Drop the ones you want
into `config/sellers.yml`:

```yaml
sellers:
  - user: spartan_auto
    store: spartautoparts
    label: Spartan Auto Parts
  - user: some_other_recycler
```

Only `user` is required — it is what goes into eBay's `_ssn=` parameter.

> I did not pre-fill 50 usernames, because inventing plausible-looking eBay
> handles would have put fake sellers in your config. `discover` gets you real
> ones in a couple of minutes.

---

## Commands

| Command | What it does |
|---|---|
| `scrape` | Walk each seller's sold listings and store them. `--sellers a,b`, `--max-pages N`, `--days 90`, `--no-cache`, `--engine playwright` |
| `discover` | Rank sellers by how often they appear in category-wide sold results |
| `parse-file` | Parse a **saved HTML page** offline. `--debug` shows every field, `--save` stores it |
| `reindex` | Re-derive make/model/part type from stored titles after editing the taxonomies |
| `report` | Write `out/report.html`; `--csv` and `--json` for the tables |
| `export` | Dump raw sold rows (title, price, shipping, date, …) to CSV. Written with a BOM so Excel on Windows reads them correctly |
| `top --by category\|make\|model\|generation\|combo\|year\|group\|seller` | Ranking table in the terminal. `--min N` sets the minimum sales per bucket (default 2 for model/generation/combo, 1 elsewhere) |
| `hot --by category\|make\|model\|generation` | Momentum: last 30 days vs the 30 before |
| `stats` | Row counts, date span, taxonomy coverage, recent runs |

---

## If eBay blocks you

A block looks like zero rows parsed, or `status: blocked` in the scrape summary.
In order of what to try:

1. **Run it from a normal residential connection.** Datacenter and cloud IPs are
   blocked aggressively — this is the single biggest factor. (For the record:
   eBay 403s this repo's own CI/dev container, so the scraper was validated
   against saved fixtures rather than live traffic.)
2. **Slow down.** Raise `delay_seconds` in `config/settings.yml` to 15–20 and
   run it overnight.
3. **`--engine playwright`.** A real browser survives some checks that a bare
   HTTP client does not.
4. **Save the page by hand and parse it.** In your browser, open the sold-search
   URL, `Ctrl+S` → "Web page, HTML only", then:

   ```bash
   python -m ebayparts parse-file ~/Downloads/page.html --debug --save
   ```

   This is also the fastest way to check the parser after an eBay redesign.

---

## How a title becomes data

```
2000-2003 CHEVY TAHOE WHEEL RIM TIRE PATHFINDER HT 245/75 R16 16X7 5 SPOKE OEM
   │        │     │     └── part: Wheel / Rim   (not "Tire" — the rule order decides)
   │        │     └──────── model: Tahoe
   │        └────────────── make: Chevrolet
   └─────────────────────── years: 2000-2003
```

Note "PATHFINDER" in that real title: it is the *tire* model. Models are only
ever matched inside the winning make's own list, so it can never be logged as a
Nissan. The make that appears **leftmost** wins, because eBay titles lead with
fitment.

Both taxonomies are plain YAML you should expect to edit:

* **`config/vehicles.yml`** — 33 makes, ~390 models, with aliases and chassis
  codes (`W222` → S-Class, `E90` → 3 Series).
* **`config/categories.yml`** — 116 part types in 19 groups. Rules are evaluated
  top to bottom, **first match wins**, so ordering carries meaning:
  `Coolant Reservoir` sits above `Radiator` precisely so
  "Radiator Coolant Overflow Reservoir Tank" lands in the right bucket.

After editing either file:

```bash
python -m ebayparts reindex     # recomputes from stored titles, no re-scraping
```

The report ends with **"Titles the taxonomy missed"** — that list is your to-do
for `categories.yml`. Watch the coverage line in `stats`; anything under ~85%
recognised means the taxonomy needs a pass before you trust the rankings.

---

## Reading the numbers honestly

* **`units` vs `revenue`** — cheap parts win on units, engines win on revenue.
  Look at both before concluding what is "hot".
* **`change_pct` is the trend signal**, not `units`. A part flat at 40/month is
  popular; one that went 12 → 40 is trending. `new` means no prior-window
  baseline, not infinite growth.
* **`revenue` excludes shipping**; `gross_with_shipping` includes it. Sellers
  price these two very differently — a $52 mirror with $16.50 shipping is a
  $69 sale.
* **This is a seller panel, not a census.** It measures what *your chosen
  sellers* sold. Add sellers until the rankings stop moving when you add more.
* **Sold ≠ paid.** eBay counts an accepted Best Offer as sold; a small share of
  those unwind.
* **Strikethrough prices** are captured as `original_price`. The green figure is
  the actual sale price and is what everything is computed from.

---

## Data model

One row per **sale** — not per item. Multi-quantity listings sell repeatedly
under one item id, so rows are keyed on `sha1(item_id | sold_date | price)`.
Re-running a scrape is idempotent; run it nightly and the 90-day window rolls
forward on its own.

```
data/parts.db      SQLite: listings + scrape_runs
data/cache/        every fetched page, so re-parsing never re-fetches
out/               report.html, report.json, csv/, listings.csv
```

Both `data/` and `out/` are gitignored — scraped data stays local.

---

## Running it on a schedule

On Windows use `scripts\run_windows.bat` with Task Scheduler (see **Install**).
On macOS/Linux:

```cron
# nightly at 03:15, then refresh the report
15 3 * * *  cd ~/scraper && .venv/bin/python -m ebayparts scrape >> scrape.log 2>&1
45 5 * * *  cd ~/scraper && .venv/bin/python -m ebayparts report --csv >> scrape.log 2>&1
```

Past ~90 days the database becomes more valuable than eBay's own search, since
it holds history eBay has already dropped.

---

## Tests

```bash
pip install pytest && python -m pytest tests/ -q
```

67 tests cover the value parsers, the make/model/part extraction (including the
Pathfinder trap), both eBay HTML layouts, store idempotency, and the analytics.
The fixtures in `tests/fixtures/` are built from the three listings in the
original screenshot.
