# GETTING_STARTED.md — end-to-end first-run guide

What to run, in what order, and how to read the results the first time you
use this tool. ~30 minutes to a working setup, then it runs itself.

Companions: `GLOSSARY.md` (every jargon term in plain English — keep it open),
`README.md` (reference), `DEMAND_RADAR.md` / `ARCHITECTURE.md` (design),
`AGENTS.md` (contributor rules).

## The four golden rules

1. **One long crawl at a time.** `--map-locality`, `--build-watchlist`,
   `--demand`, `--store-inventory` all hit the same apps; running two gets
   both rate-limited into empty results. The dashboard Features panel shows
   what's running — check before starting another.
2. **The dashboard is the control room.** Everything long-running is best
   started/stopped from **Features** (they run as managed children;
   STOP ALL reaps them). Terminal runs work too, but you own the process.
3. **Config edits apply on next start.** Editing `config.yaml` does nothing
   to a running feature until you stop/start it.
4. **Absence of evidence ≠ evidence.** Unknown stock is recorded as NULL,
   never "out of stock"; a product that vanished from the app is a
   `vanished` event, not `oos`; a blank heatmap cell means *we weren't
   watching*, not *all was fine*.

## Stage 0 — Install & sanity (5 min, offline)

    cd <repo>
    python3 run.py --check     # config parses (both YAML loaders), adapters import
    python3 run.py --demo      # full pipeline against fake data

`--check` must print `config ok; stations: N`. `--demo` runs the whole
detect→alert pipeline **offline** with an *injected fake glitch* — if you see
an alert for it, your detection engine works. Nothing in demo mode is real;
never interpret demo output as market data.

Optional pieces: Telegram push needs `TG_BOT_TOKEN`/`TG_CHAT_ID` in `.env`
(copy `.env.example`); the AI panel needs `AI_API_KEY` + the `ai:` block.
The browser layer needs no install (Playwright + Chromium are pre-installed
elsewhere and wired via `anti_block.*` in config).

## Stage 1 — Open the dashboard (2 min)

    python3 run.py --ui            # http://127.0.0.1:8787 — dashboard ONLY,
                                    # starts no loops; add --bot and/or --monitor
                                    # to also run them, or start loops from
                                    # Features below

Tour, left to right: **Status** (live crawl events + alerts), **Categories**
(what your crawls actually cover), **Demand Radar** (DPI table, onset
heatmap, ETA curves), **Searches** (bot history), **SQL databases**
(read-only browser over `deals.db` + `inventory_*.db`), **Features**
(start/stop/log for every job), **Location** editor, **AI** panel.

Interpretation: the dashboard only *reads* the database — if a panel is
empty, the feature that fills it isn't running yet. Start with Features.

## Stage 2 — The deals finder (glitch monitor)

Fastest real-data win. Either: dashboard **Features ▸ Glitch monitor ▸
Start**, or one cycle from the terminal:

    python3 run.py --once          # single corridor pass, then exits

Each cycle crawls every station × app (home page + honey-pot searches + one
rotating `schedule.crawl_terms` query). What to watch:

- **Live feed**: `blinkit @ Andheri — 214 products, 8.2s` = healthy.
  `products=0` on an app that usually returns hundreds = WAF gate or
  soft-block; the failover ledger will show `gate`/`boost` events.
- **Alerts**: a price far from the store's own baseline or the honey-pot
  true price. Interpretation: *local* — a glitch at store 34292 says
  nothing about store 36517. Cross-check the link in the app before
  believing (or buying).
- **Categories panel**: share of observations per product category. If
  "Dairy & Eggs" dominates, that's the apps' dairy-first home carousels —
  tune `schedule.crawl_terms` / `anti_block.honey_pot` toward your basket.

Sanity query: `sqlite3 deals.db "SELECT ts, sku_key, reason, price FROM
alerts ORDER BY ts DESC LIMIT 10;"`

## Stage 3 — Price search (instant, no Telegram needed)

    python3 run.py --search "cigarettes"       # CLI across QC platforms
    python3 run.py --bot                       # Telegram bot + monitor together

The reply ranks by **effective price** = item + delivery fee + best code
from `codes.yaml`. Interpretation: edit `codes.yaml` to the cards/codes you
actually hold, or the "effective" number is fiction. Offers show their note
so you can verify at checkout.

Bot extras: `/watch <product>` pings this chat when any crawl/search
surfaces a match (rate-capped, 6h cooldown per watch); `/digest` = today's
cheapest per category + DPI top-5, straight from the DB, no crawling.

## Stage 4 — Demand Radar (the pipeline — strict order)

Three phases build on each other; each writes what the next reads. Times
assume the default config (Andheri West, blinkit+zepto+instamart).

### 4a. Map the darkstores (once per locality, ~10–30 min)

    python3 run.py --map-locality                  # all apps
    python3 run.py --map-locality --apps blinkit   # or one app

Probes a landmark+GPS-anchor grid; each anchor resolves to the nearest
darkstore, so clusters of anchors = one store's catchment. Writes the
`darkstores` table + `exports/locality_current_<app>.json` (rotation pools).

Interpretation: the end summary lists stores with ETA. A store id that
appears under anchors 800m apart is normal (big catchment). `products=0`
with a resolved store = the app gated the session, not that the store is
empty.

### 4b. Build the watchlists (once per store set, ~2–5 min/store)

    python3 run.py --build-watchlist
    python3 run.py --build-watchlist --apps blinkit --store 47578

Deep-sweeps each store in ONE browser session: home feed → category pages
(`demand.categories_per_store`, minus any `skip_categories` labels) →
search terms (`demand.staple_queries`). Every SKU is scored and stored in
`watchlist` with its collection labels.

Interpretation: `[watchlist] blinkit/34292: 1834 SKUs seen (1790
stock-stamped, 212 OOS at build) -> active=1834`. "OOS at build" is a
baseline, not news. With `unbiased_harvest: true` everything stays active —
the memory, not the probe list. Re-run after you change `staple_queries`
or `skip_categories`, or when the catalog drifts (weekly is plenty).

### 4c. Probe stock (continuous — this is the one you leave running)

    python3 run.py --demand                        # loop
    python3 run.py --demand --once --max-terms 5   # quick single round (~70s)

Each cycle re-sweeps a store (home + categories + its top watchlist search
terms) and writes `stock_obs`. Debounced: N consecutive zero-reads
(`oos_debounce_snapshots`) open an `oos` event; one in-stock read closes
it; SKUs missing from M successful sweeps become `vanished`. Canary queries
(`stock_canary_queries`) must return in-stock items — if milk shows OOS
everywhere, the machine *freezes* (suspect cycle) instead of inventing
demand.

### 4d. Read the results

    python3 run.py --demand-report            # terminal
    python3 run.py --demand-report --csv      # + exports/dpi_<date>.csv

or the dashboard Demand panels. How to interpret each:

- **DPI (Demand Pressure Index)** = Σ OOS-minutes × recency ÷ days
  observed. High = keeps going out of stock, lately. It is a *proxy for
  demand* — we see shelves, never order volumes. A SKU observed for only
  two days with one long outage can outrank a month of data: check the
  obs-days column before acting.
- **Onset heatmap (hour × SKU)**: where stock-outs *begin*. Read columns
  for "evening collapse" patterns; read rows for "this SKU lives on the
  edge". **Blank = prober wasn't running then.**
- **ETA curve**: median delivery minutes per hour per store. Rising ETA
  and rising OOS together = store strain; ETA alone = traffic/rain.
- **oos vs vanished**: `vanished` = delisted/hidden (assortment change),
  `oos` = listed but unavailable (demand or supply). Different stories —
  don't merge them.

Useful queries:

    sqlite3 deals.db "SELECT sku_key, COUNT(*) n FROM oos_events WHERE kind='oos' GROUP BY 1 ORDER BY n DESC LIMIT 10;"
    sqlite3 deals.db "SELECT store_id, COUNT(*), MAX(eta_min) FROM stock_obs WHERE ts>strftime('%s','now')-86400 GROUP BY 1;"

## Stage 5 — The AI assistant (optional, after Stage 4 has data)

Dashboard **AI panel** → *Understand the results*. The model receives a
compact **digest** of the DB (DPI, heatmap, ETA, counts) — it does not
crawl and only knows what's in that digest. Two practical notes learned the
hard way:

- Long answers can hit the panel's limits, so every analysis is also saved
  as a downloadable markdown report (`exports/ai_explain_*.md`); follow-up
  Q&As are appended to the same file.
- Free-tier providers rate-limit (HTTP 429): wait a few seconds between
  analyses.

The panel can also *propose* config changes (`/ai/methodology`,
`/ai/focus`) — suggestions are enforced against a whitelist; you approve
before anything is written.

## One-shots worth knowing

    python3 run.py --qc-status          # are the apps serving us at all? (~1 min)
    python3 run.py --store-inventory    # what stores near YOUR real location stock
                                        # (separate inventory_<app>.db files)
                                        # RUN ALONE — stop other crawls first
    python3 scripts/live_sweep.py       # hunt real glitches across the corridor now

## The daily rhythm (what to actually leave running)

- **Always-on**: dashboard (`--ui`) + Features ▸ Glitch monitor + Features
  ▸ Demand prober. That's the whole product.
- **Weekly**: re-run `--build-watchlist` (catalogs drift); skim
  `--demand-report` or the Demand panels.
- **When something looks wrong**: `--qc-status` first (is it us or the
  apps?), then the feature's log in the panel.

## Symptom → meaning → fix

| Symptom | Meaning | Fix |
|---|---|---|
| `products=0` everywhere, stores resolve | fresh sessions rate-limited (two crawls at once, or IP reputation) | stop all crawls, wait a cycle, restart ONE |
| One app silent for many cycles | WAF-gated; failover is leaning on Blinkit | nothing — cooldown is automatic; check `--qc-status` |
| "suspect cycle" / machine frozen | canaries OOS or mass in→out flip = soft-block suspected | let it ride; if hours long, check IP/proxy |
| Heatmap mostly blank | prober wasn't running those hours | start Demand prober; blanks are not "all fine" |
| DPI dominated by one SKU for days | could be real, or a zombie event from stale coverage | check obs-days + last real sighting in stock_obs |
| AI panel errors | no key / 429 rate limit | check `ai:` config + `.env`; wait and retry |
| Alerts stopped entirely | rate cap (`alert.max_alerts_per_hour`) or monitor down | Features ▸ monitor log |
| Dashboard shows stale numbers | feature restarted but dashboard didn't respawn it… or config not applied | stop/start the feature; refresh |

## Where the data lives

Everything is one sqlite file: `deals.db` — `price_obs` (every price seen),
`alerts` (glitches), `darkstores`, `watchlist` (SKU memory), `stock_obs`
(every stock read), `oos_events` (confirmed stock-outs/vanishings),
`searches`/`search_results` (bot history), `keyword_watches`. The dashboard
**SQL databases** panel browses all of it read-only. To start completely
fresh: stop all features, move `deals.db` aside, restart — history is never
required, but every interpretation above needs *some* accumulation first.
