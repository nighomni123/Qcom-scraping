# Moneymaker v2 — Glitch & Deal Monitor

Two features in one tool:

1. **Glitch monitor** — watches Blinkit/Instamart/Zepto across the Mumbai
   corridor (Virar → Andheri) for pricing glitches, upstream of Telegram groups.
2. **Price-search bot** — message a Telegram bot any product name and get the
   cheapest offer across Blinkit, Zepto, Instamart, Amazon & Flipkart,
   including delivery fees and applicable codes/offers. Both run simultaneously.
3. **Demand Radar** (new) — maps the darkstores serving a locality (e.g. Andheri
   West), then tracks per-store in/out-of-stock + delivery ETA over time to
   build a chronological demand-pressure heatmap. See `DEMAND_RADAR.md`.

See `ARCHITECTURE.md` for the design and the anti-block strategy.

## Demand Radar (phases 0–3 live)

    python3 run.py --map-locality                    # discover Andheri West darkstores
    python3 run.py --map-locality --apps blinkit     # one app only
    python3 run.py --map-locality --max-points 5     # quick partial sweep
    python3 run.py --store-inventory                 # per-app store DBs near YOU
    python3 run.py --store-inventory --apps blinkit,zepto --max-points 8
    python3 run.py --store-inventory --lat 19.07 --lon 72.88       # override point
    python3 run.py --build-watchlist                 # per-store SKU probe set
    python3 run.py --build-watchlist --apps blinkit --store 47578 \
                    --max-per-store 100 --max-queries 10
    python3 run.py --demand                          # continuous stock probing loop
    python3 run.py --demand --once --apps blinkit --store 47578   # single round
    python3 run.py --demand-report --csv             # DPI ranking + heatmap summary
    python3 run.py --qc-status                       # per-app QC health board

**Live progress:** every browser sweep (`--build-watchlist`, `--map-locality`,
`--store-inventory`, `--demand`) streams its progress as it works — which store
it's on as `(i/N) <app> @ store <id> — <label>`, how many visits are queued
(categories + searches), then one `[sweep] …` line per visit with the
cumulative SKU count, plus onboarding/localization steps. Watch it in the
terminal, or start the feature from the dashboard's **Features** panel and
open its **log** (auto-refreshes ~1.2 s). Mute with
`anti_block.stream_progress: false` in `config.yaml`.

**Store inventory near you (`--store-inventory`):** resolves the machine's
approximate location from its public IP (honest — no GPS spoofing; override
with `--lat/--lon`), builds a small anchor grid around it, and maps each app's
darkstores into SEPARATE databases: `inventory_blinkit.db`,
`inventory_instamart.db`, `inventory_zepto.db` (each a full Store schema; the
`darkstores` table holds that app's stores with label/coords/ETA). Every probe
is product-bearing and CAPTURED: each store-attributed product lands in that
app's DB as `stock_obs` (stock/price/mrp/eta, `source='inventory'`) +
`price_obs` (name/price/url, auto-categorized) — so the SQL databases panel in
the dashboard shows what each nearby store actually stocks. One-shot probes
use the probe routes: Zepto probes its search route, Blinkit/Instamart fire a
few staple searches in-session so you get more than the dairy-first home
carousels, and Instamart's location-consent gate is driven through the app's
own UI buttons when a session is stuck at zero products. **Run it alone:**
starting it while `--demand` / `--map-locality` / `--build-watchlist` is also
crawling the same apps gets the fresh sessions rate-limited into empty probes
(stores resolve, products = 0). Stop other crawls first (Features panel),
then start the inventory.

**Demand analysis (phase 4):** the dashboard's Demand Radar panels show a
Demand Pressure Index per SKU (Σ OOS-minutes × recency ÷ observation-days),
a hour×SKU stock-out onset heatmap (local time — when demand spikes), and
per-hour delivery-ETA curves. `--demand-report` prints the same in the
terminal; `--csv` exports `exports/dpi_<date>.csv`.

**QC-first defaults** (since 08-22): amazon/flipkart tracking is disabled and
the search bot compares quick-commerce platforms only — re-enable via
`adapters.trackers` / `search.platforms` in `config.yaml`.

`--map-locality` resolves each app's distinct darkstores by probing a
landmark+grid anchor set (config → `demand.locality`), enforcing our GPS on
every intercepted API call (cached-location seeding + request rewrite).
`--build-watchlist` deep-sweeps each store in ONE browser session — home feed,
category click-throughs, staple searches — scoring every SKU (search hits >
home presence) into the `watchlist` table. `--demand` then loops: each cycle
re-sweeps a store's watchlist collections and writes `stock_obs`; debounced
stock-outs become `oos_events` (restart-proof; vanished SKUs get their own
event kind; canary/mass-flip guards freeze the machine on suspected
soft-blocks instead of faking demand). Query it:

    sqlite3 deals.db "SELECT sku_key, COUNT(*) FROM oos_events
                      WHERE kind='oos' GROUP BY sku_key ORDER BY 2 DESC;"

## Price-search bot

    python3 run.py --bot            # telegram bot + monitor, simultaneously
    python3 run.py --bot --no-monitor   # bot only
    python3 run.py --search "amul milk" # CLI, no telegram needed

Setup the bot once:
1. Talk to [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token.
2. Put it in `.env` as `TG_BOT_TOKEN=...`.
3. Start `python3 run.py --bot`, open your bot in Telegram, send it a product name.

Reply looks like:

```
🔎 “amul milk” — cheapest across platforms

🥇 BLINKIT — Amul Taaza Toned Milk 500ml
     ₹17 (MRP ₹17)
     → ₹42 effective  (+₹25 delivery)
🥈 FLIPKART — Amul A2 Buffalo Milk, 1 L
     ₹70
     → ₹103 effective  (code AXISFK 10% −₹7, +₹40 delivery)
...
```

Offers/delivery fees live in `codes.yaml` — **edit it** to match the cards and
codes you actually hold; every applied offer shows its note so you can verify
at checkout before paying.

### Proactive alerts: /watch & /digest

    👀 /watch amul milk     # ping this chat when crawls/searches surface a match
    👀 /watch               # list your watches
    🗑 /unwatch amul milk   # stop watching
    📋 /digest              # today's cheapest find per category + DPI top-5

Watches persist in `deals.db` (the `keyword_watches` table, up to 10 per chat).
Every `--search` run, every bot search, and every monitor/demo crawl batch is
matched against them — same token-overlap scoring as search ranking — and any
match is pushed straight to your Telegram chat with platform, price and link.
Pushes are rate-limited by `alert.rate_cap` per hour plus a 6-hour per-watch
cooldown (`last_alerted_ts`), so restarts and hot keywords can't spam you.
`/digest` reads today's archived search results + the Demand Radar DPI ranking
straight from the DB — no crawling.

## How it beats polling limits

- **Browser-intercept, not web scraping.** Runs the *real* mobile web app in
  headless Chromium. JS challenges that block `curl` are solved by the browser.
- **The app is the API docs.** We intercept the app's own signed catalog calls
  and replay them on a tight loop. When the app rotates paths/headers, the next
  page load re-discovers them.
- **Geo-anchored store walking.** Each Mumbai station coordinate resolves a
  *different* dark store; glitches are local, so we watch the corridor and treat
  each store as its own baseline.
- **Rotated identity.** Fresh mobile UA + install-id + (optional) residential
  proxy per cycle.
- **Honey-pot canaries.** Hardcoded TRUE prices for a few high-velocity SKUs;
  any large deviation is an instant glitch flag — no ML needed for the first pass.
- **Off-peak speedup.** Crawls *faster* at night when server rate limits are
  looser — inverts the usual bot-burst signature.

## Setup

    cd ~/Documents/Projects/Moneymaker
    pip install pyyaml            # optional; a stdlib fallback parser is built in

    # Browser layer uses the PRE-INSTALLED Playwright + Chromium (no install needed):
    #   ../Do not delete folder/node_modules  +  ../Do not delete folder/.pw-browsers
    #   (paths configured in config.yaml → anti_block.node_path / pw_browsers_path)

    cp .env.example .env            # fill TG_BOT_TOKEN / TG_CHAT_ID for phone push
    python3 run.py --check          # validate config + adapters
    python3 run.py --demo           # end-to-end pipeline with an injected glitch
    python3 scripts/live_sweep.py   # hunt REAL glitches across the corridor now
    python3 run.py                  # continuous loop

Without Node/Chromium available the QC adapters degrade gracefully (return no
data) and the trackers + detection engine still run. Demo mode needs nothing
external.

## Config (`config.yaml`)

- `geo.corridor` — Mumbai stations Virar→Andheri with lat/lon. Edit to widen.
- `anti_block.honey_pot` — canary SKUs with `true_price`. Tune to your basket.
- `detect.*` — glitch thresholds (deviation %, z-score, MRP margin).
- `alert.*` — desktop notification, optional Telegram, log file, rate cap
  (`max_alerts_per_hour` for glitch alerts; `rate_cap` for /watch pushes).
- `demand.*` — Demand Radar: `locality` (name/bbox/landmarks/grid), watchlist
  builder (`categories_per_store`, `staple_queries`, `watchlist_max_per_store`),
  prober (`probe_interval_sec`, `probe_terms_max`, `oos_debounce_snapshots`,
  `vanished_cycles`, `stock_canary_queries`, suspect-flip guards).

## Secrets

Never commit `.env`. The old repo accidentally had a Gmail app-password in
`config.json` — this version keeps all secrets in `.env` (git-ignored).

## Output

- macOS notification + optional Telegram push.
- `deals.log` — every alert.
- `deals.db` — full price history + alerts + the complete search archive
  (`searches` / `search_results` tables; every bot/UI/CLI search lands there,
  tagged with `src/categories.py` product categories for future analysis),
  plus Demand Radar tables (`darkstores`, `watchlist`, `stock_obs`,
  `oos_events`) and Telegram keyword watches (`keyword_watches`).
- `exports/locality_<name>.json` — darkstore map with per-store rotation
  pools of anchor coordinates.

## Live dashboard (`--ui`, http://127.0.0.1:8787)

Real-time view of everything the process does, plus the exit switch:

- **Features** — every capability of this repo as a card (glitch monitor,
  Telegram bot, demand prober, demo, QC probe, inventory sweep, locality
  mapping, watchlist builder, demand report). Start and stop them right from
  the browser; each runs as a managed child process with live logs, uptime
  and exit codes. STOP ALL / shutdown also reaps anything the dashboard
  started, so nothing keeps crawling in the background.
- **Working area** — change WHERE the tool operates, interactively: drag the
  map pin (or click the map / pick a preset / use your device location / the
  server's IP location) and save it as the Demand Radar locality with a
  radius slider; edit the monitor's corridor stations in a table and set the
  search anchor. Every save is validated against both YAML loaders before it
  lands in config.yaml (previous file backed up in /tmp) and applies when you
  next start or restart a feature.
- **AI assistant (optional)** — three helpers on top of live data:
  *Understand the results* explains what the Demand Radar panels show (pressure
  SKUs, temporal patterns, data-quality caveats) — the full analysis is also
  saved as a markdown file (`exports/ai_explain_<stamp>.md`) with a
  **⬇ download full report** link under the panel (served by
  `GET /ai/report/<file>`), so long answers never depend on the dashboard
  box; *Tune probing methodology*
  reviews probing health and proposes adjustments to the demand-prober knobs,
  which you accept/reject per item before anything is written; *Choose product
  focus* turns intents like "beverages and baby care" into concrete search
  queries applied to `demand.staple_queries`. Config writes go through the
  same validated path as the location editor. Setup: put `AI_API_KEY=…` in
  `.env` (any OpenAI-compatible API; model/base via `config.yaml → ai:`), or
  point `ai.base_url` at a keyless local Ollama
  (`http://127.0.0.1:11434/v1`). With `ai.enabled: false` (or no key) the
  panel greyed-out says exactly why.
- **SQL databases** — browse every sqlite file in the repo (deals.db +
  `inventory_blinkit.db` / `inventory_instamart.db` / `inventory_zepto.db`):
  pick a database, pick a table, see row counts and the newest rows. The
  same data is available as JSON: `GET /db`, `GET /db/<db>/<table>`.
- **Live feed** — every crawl, cycle, alert, and Telegram query as it happens.
  **Click any row** for details: full ranked results of a search (with codes,
  delivery, links), sample products of a crawl, or an alert's reason/store.
- **Product categories covered** — all crawled products grouped by category.
- **Search history** — every archived search; click to reopen its stored
  results from `deals.db` anytime.
- **Demand Radar panels** — platform health, DPI ranking, onset heatmap,
  per-hour ETA curve.
- **■ STOP ALL** — gracefully ends the dashboard and everything it manages.
