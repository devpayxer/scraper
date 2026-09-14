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
2. **If you scrape anyway, ask for as little as possible.** That is what the
   pacing design below is for.

Also note: **eBay only publishes ~90 days of sold history**, which happens to be
exactly the window you asked for. There is no way to reach further back through
search — anything longer has to be accumulated by running this on a schedule and
letting the database grow.

---

## The automatic route that does not get blocked

If you tried the scraping routes and eBay refused them (it will, from most home
and datacenter connections), stop fighting the website. The way to get sold data
that **updates on a schedule and never hits a captcha** is a data-API vendor: they
do the scraping on their side and hand you clean JSON. You pay them a small
monthly fee instead of fighting eBay yourself.

```bash
# 1. Sign up with a vendor and get an API key. Options that return sold data:
#      - OpenWeb Ninja "Real-Time eBay Data" (via RapidAPI, has a free tier)
#      - SoldComps (sold listings, 8 marketplaces)
#      - an Apify eBay-sold-listings actor (dataset -> JSON)
#      - eBay's own Marketplace Insights API, if you are granted access
#
# 2. Put your key in an environment variable (never in a file):
setx EBAY_DATA_API_KEY "your-key-here"        # Windows; open a NEW terminal after

# 3. Describe the vendor in config/settings.yml (data_api block) -- base URL and
#    a field_map from their JSON to ours. There is a worked example in the file.
#
# 4. Confirm your field_map is right, with no key and no network, using one saved
#    response from the vendor's API playground:
python -m ebayparts apitest saved_response.json

# 5. Pull, on demand or on a schedule:
python -m ebayparts apipull
python -m ebayparts report
```

`apipull` reuses everything else unchanged -- the make/model/part enrichment, the
90-day window, dedupe, and the report all run on API rows exactly as on scraped
ones. Point Windows Task Scheduler at `scripts\run_windows.bat` (which now calls
`apipull`) and it updates itself daily with no browser and nothing to get blocked.

**Why this and not scraping:** eBay actively defends its site, so any scraper you
run will keep breaking. A data vendor absorbs that fight as their business. Free +
automatic + sold data do not exist together; this trades a few dollars a month for
"set it and forget it".

The queries you pull are just the categories you want to study:

```yaml
queries:
  - q: "headlight"
  - q: "tail light"
  - q: "bumper"
```

---

## How this stays quiet

The thing that gets traffic flagged is **volume and rhythm**, not headers. A
naive sweep of 50 sellers × 40 pages is 2,000 requests in one burst; no person
browses like that, and no amount of header tuning disguises it. So instead of
trying to look human, this collector *asks for less*:

| | |
|---|---|
| **Daily budget** | A hard cap (default **80 pages/day**) counted in the database, so two runs on the same day cannot double the traffic |
| **Seller rotation** | Each run touches **6 sellers**, least-recently-visited first. A 50-seller panel comes round over ~8 runs |
| **Incremental stop** | A seller is dropped after **2 pages with nothing new**. Once backfilled, a daily check is 1–2 pages per seller |
| **Uneven gaps** | **25–60s** between pages, plus a ~4 min break every 12 pages |
| **Waking hours** | Runs only between **08:00–23:00** local; outside that it declines and exits |
| **Stops on refusal** | One 403 ends the day. It never retries into a wall |
| **Cache** | Re-runs and re-parses never re-fetch |

Steady state after backfill is roughly **10–20 pages per day, a few minutes** —
comparable to one person idly checking a few storefronts.

### Coverage warnings

The one way this design can lose data permanently: eBay drops sales at ~90 days,
so a seller left unvisited that long has sales that no longer exist to collect.
Rotation makes that unlikely (a 50-seller panel comes round every ~9 runs, well
inside the window), but a PC switched off for months, or a seller that gets
blocked every single run, would drift there quietly.

So `plan` and `stats` both flag it:

```
Coverage warnings:
  seller           last collected  days ago  rows   status
  --------------------------------------------------------
  gulf_salvage         2026-05-21       104  4,102      gap
  midwest_oem          2026-06-16        78  2,880  at risk
  atx_used_parts       2026-07-12        52  1,455    stale
  rocky_mtn_parts               -         -      0  pending

  1 seller(s) have not been collected in 90+ days. eBay has already dropped
  the sales from that gap -- they cannot be recovered by scraping harder now.
```

Bands scale with `lookback_days`: **ok** under half the window, **stale** past
half, **at risk** past 80%, **gap** past the whole thing. A seller blocked 3+
runs in a row is flagged too — it is being attempted but never actually
collected, which a staleness check alone would miss.

Nothing here is measured on attempts. It is measured on *successful* collection,
because a seller that gets tried and refused every run is not being collected,
however busy the log looks.

See exactly what a run would do, without touching the network:

```bash
python -m ebayparts plan
```

```
Mode           : passive
Panel          : 50 seller(s), 6 per run -> full sweep every 9 run(s)
Pages          : up to 3/seller, stops after 2 page(s) with nothing new
Gap            : 25-60s, plus a 240s break every 12 pages
Active hours   : 08:00-23:00 (now inside)
Budget today   : 12/80 used, 68 left
This run       : at most 18 page(s), roughly 13 min
```

### The first 90-day pull

The initial backfill is the only heavy part, and it is spread over days rather
than done in one sitting:

```bash
python -m ebayparts scrape --mode backfill    # run once a day until it settles
```

Each run spends its budget, remembers where it stopped, and resumes tomorrow.
With 50 sellers expect roughly a week to fill in. There is no rush — eBay serves
the same 90-day window whenever you next ask, so a slow backfill loses nothing
except the wait.

Then leave it on `passive` (the default) forever.

### What this deliberately does *not* do

No proxy rotation, no user-agent churn, no CAPTCHA solving, no headless-browser
stealth patches. Those are evasion rather than restraint — they raise the stakes
if anyone does look, and they do nothing about the volume that would attract the
look in the first place. The one concession is `curl_cffi`'s Chrome TLS
fingerprint, without which eBay refuses even a single request.

If you get blocked anyway, the honest read is that eBay does not want this
traffic. Raise the delays, cut `sellers_per_run`, or move to the official API —
do not reach for a proxy pool.

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
| `scrape` | One paced run: a few sellers, a capped number of pages. `--mode backfill` for the initial pull, `--sellers a,b`, `--max-pages N`, `--ignore-hours`, `--no-cache`, `--engine playwright` |
| `apipull` | **Pull sold data from a data API — the automatic, unblocked route.** `--query`, `--max-pages` |
| `apitest` | Test your `data_api` field_map against a saved JSON response, no key needed |
| `plan` | Show what the next run would do — budget, queue, timing. Touches nothing |
| `probe` | Find a browser profile eBay answers on your connection. `--list` shows all targets |
| `browser` | Open the persistent browser profile once, by hand (cookie banner, region) |
| `urls` | Print the sold pages to save from your browser. `--open` opens them as tabs, `--out` writes a clickable index |
| `import` | Parse pages you saved into `data/inbox`. `--archive` files away the ones that worked |
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

**Start here:** find a client configuration your connection gets answers with.

```bash
python -m ebayparts probe
```

It tries a handful of current browser profiles against one eBay URL, spaced a
few seconds apart, and tells you what came back:

```
  profile    warm-up  http   result  items   page / error
  ------------------------------------------------------------
  chrome150       no   200       ok      60   spartan_auto | eBay

  chrome150 works (60 listings parsed).
  Put this in config/settings.yml:
    impersonate: chrome150
    warm_up: false
```

If nothing gets through, the probe distinguishes the two cases that matter:
**refused** (eBay answered and said no — your IP or session is unwelcome, and no
client setting will change that) versus **unreachable** (nothing answered —
check the connection, VPN or firewall).

`impersonate` must name a *current* browser. The default is `chrome146`;
`probe --list` shows all 54 targets. A two-year-old profile is worse than
useless — hardly any real Chrome 124 is still browsing, so claiming to be one
is itself the anomaly.

### When eBay refuses outright: drive a real browser

`probe` reporting **refused** across every profile means eBay will not serve an
HTTP client from your connection, however it is dressed. A real browser is a
different proposition, and it is the route that has a chance:

```bash
pip install playwright && playwright install chromium

python -m ebayparts browser                        # one-off: accept cookies, close
python -m ebayparts scrape --engine playwright --max-pages 2
```

The browser is **visible by default** and uses a **persistent profile**
(`data/browser-profile`), so cookies survive between runs and the second visit
looks like a returning visitor rather than a fresh browser every time. You can
watch what it does, and closing the window stops it.

All the passive pacing still applies — daily budget, seller rotation, the
early stop, active hours. Only the fetching changed.

**Two things worth weighing:**

* **Do not sign in to your selling account.** An IP block is recoverable;
  automated activity tied to a selling account is a bigger problem. The profile
  works fine signed out.
* This is still automated access under eBay's User Agreement. It is not
  disguised — see below — but it is not sanctioned either.

**What this deliberately is not:** there is no `--disable-blink-features=
AutomationControlled`, no `navigator.webdriver` masking, no stealth plugin, no
scripted mouse jiggling. It is an honest Playwright Chromium. If eBay declines
to serve automation, that is an answer rather than an obstacle to route around,
and the run stops.

### When even that is refused

If `probe` reports **refused** across every profile, eBay does not want
automated requests from your connection, and no setting in here changes that.
The route that still works — and stays plainly above board, because it is just
you browsing:

```bash
python -m ebayparts urls --open      # opens each seller's sold page as a tab
#   in each tab: Ctrl+S -> "Webpage, HTML Only" -> save into data/inbox
python -m ebayparts import --archive # parses everything you saved
python -m ebayparts report
```

`import` tells you honestly what it read. A captcha page saved by mistake is
reported as `challenge`, not silently counted as an empty store, and files that
failed stay in the inbox for a retry while successful ones are archived by date.

Everything downstream — rotation, dedupe, the 90-day window, the report — works
identically on imported pages. Only the fetching changes.

One page holds 240 sold listings, so most sellers need a single save. A 50-seller
panel is 50 tabs once, then a handful of re-saves a week for the busy ones.

### Other things to try

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
# one paced run mid-morning, report after. Keep it inside active_hours --
# a 03:15 cron would simply decline to run.
20 10 * * *  cd ~/scraper && .venv/bin/python -m ebayparts scrape >> scrape.log 2>&1
50 10 * * *  cd ~/scraper && .venv/bin/python -m ebayparts report --csv >> scrape.log 2>&1
```

Once a day is plenty, and missing days costs nothing.

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
