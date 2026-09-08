# qcom-scraping v2 — Glitch & Deal Monitor

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
First time here? `GUIDE.md` walks you through what to run, in what order,
and how to interpret every result — with every jargon term (OOS, DPI,
darkstore, honey pot, WAF…) defined in plain English where it first appears.


## Demand Radar (phases 0–3 live)

    python3 run.py --map-locality                    # discover Andheri West darkstores
    python3 run.py --map-locality --apps blinkit     # one app only
    python3 run.py --map-locality --max-points 5     # quick partial sweep
    python3 run.py --store-inventory --app blinkit --store 34292            # one store, full-category capture
    python3 run.py --store-inventory --app zepto --store Z9 --lat 19.07 --lon 72.88  # any store, anywhere
    python3 run.py --product-fields                                       # grocery name parser self-test
    python3 run.py --product-fields --report --sample 200                 # accuracy CSV (exports/product_fields_sample.csv)
    python3 run.py --product-space --apps blinkit,zepto,instamart --csv   # normalized union -> exports/product_space.csv
    python3 run.py --product-vectors --csv              # M2 per-group attribute+commercial vectors
    python3 run.py --detect-assortment-gaps --csv       # M3 cross-app assortment gaps
    python3 run.py --product-density --csv              # M4 per-category density + coverage guards
    python3 run.py --detect-gaps --csv                  # M5 internal/attribute gaps
    python3 run.py --score-opportunities --persist      # M6 scoring + M7 persist to deals.db
    python3 run.py --opportunity-report --limit 10      # latest persisted opportunity snapshot
    python3 run.py --temporal-analysis --min-n 25        # temporal signals + category stability report
    python3 run.py --validate-opportunities --limit 20   # top persisted opportunities for human review
    python3 run.py --opportunity-pipeline --force        # incremental refresh (full refresh with --force)
    # --min-n honored by: product-density, detect-gaps, score-opportunities, temporal-analysis (stability)
    python3 run.py --build-watchlist                 # per-store SKU probe set
    python3 run.py --build-watchlist --apps blinkit --store 47578 \
                    --max-per-store 100 --max-queries 10
    python3 run.py --build-watchlist --catalog --apps blinkit --store 34292
                                                    # catalog-inventory snapshot:
                                                    # every category, no search
                                                    # terms (~20-60 min, run ALONE)
    python3 run.py --catalog-report [--store 34292]  # snapshot history + new/delisted churn
    python3 run.py --embed-catalog [--limit N]      # one-time semantic backfill:
                                    # vectorize every distinct product name
                                    # (NVIDIA-hosted nvidia/llama-nemotron-
                                    # embed-vl-1b-v2 @2048 dims via
                                    # ai.embedding_base_url +
                                    # NVIDIA_Build_API_KEY; RESUMABLE — re-run
                                    # to continue, cached names are skipped).
                                    # LOCAL OPT-IN ALTERNATIVE (zero quota):
                                    # set ai.embedding_provider: "ollama" ->
                                    # workspace-local Ollama +
                                    # embeddinggemma-300m @768 dims. Harness:
                                    # scripts/test_embeddinggemma.py (starts
                                    # the server + pulls the model itself);
                                    # quality comparison vs NVIDIA:
                                    # scripts/compare_embeddings.py (numpy in
                                    # .venv/ — run with .venv/bin/python);
                                    # batch-size benchmark:
                                    # scripts/bench_ollama_batch.py (batch 256
                                    # is the measured optimum on this box)
    python3 run.py --similar <phrase> [--limit 10]  # semantic archive query:
                                    # most similar archived names (needs
                                    # --embed-catalog first)
    .venv/bin/python scripts/embed_3d_map.py [--model nemotron|gemma] [--k 24]
                                    # Inventory Atlas: self-contained dark-themed
                                    # HTML explorer of the embedding archive ->
                                    # exports/embedding_map[_<model>].html
                                    # (3D semantic map + 2D overview + cluster
                                    # explorer + product inspector; Semantic /
                                    # Price / Store / Category / Density modes;
                                    # needs sklearn/plotly/scipy in .venv/;
                                    # --projection umap needs umap-learn;
                                    # --sample N renders N random names for
                                    # fast UI testing)
    python3 run.py --demand                          # continuous stock probing loop
    python3 run.py --demand --once --apps blinkit --store 47578   # single round
    python3 run.py --demand-report --csv             # DPI ranking + heatmap summary
    python3 run.py --purge-vouchers [--dry-run]      # wipe voucher/gift-card rows from Demand Radar tables
    python3 run.py --qc-status                       # per-app QC health board

**Silent-hour scheduling (09-02):** the `--demand` loop only *starts* rounds in
the hours listed in `demand.sweep_windows` (local time, `"0-23"` = all hours =
no change). The Aug 24–Sep 1 `stock_obs` history had **zero observations in
hours 02–10 and 15–16**, so OOS onsets that began in those windows were only
recorded at the next sweep (the onset heatmap's `started_at` is the first read,
not the true onset) and the heatmap is biased to sweep hours. Leave the Demand
prober feature running 24/7 to cover every hour, or set e.g.
`demand.sweep_windows: ["2-10", "15-16"]` to spend the crawl budget *only* on
the historically-silent windows. The loop idles (no crawls, no rate-limit burn)
outside the windows and resumes at the next in-window hour; a round already in
flight always finishes.

**Live progress:** every browser sweep (`--build-watchlist`, `--map-locality`,
`--store-inventory`, `--demand`) streams its progress as it works — which store
it's on as `(i/N) <app> @ store <id> — <label>`, how many visits are queued
(categories + searches), then one `[sweep] …` line per visit with the
cumulative SKU count, plus onboarding/localization steps. Watch it in the
terminal, or start the feature from the dashboard's **Features** panel and
open its **log** (auto-refreshes ~1.2 s). Mute with
`anti_block.stream_progress: false` in `config.yaml`.

**Store inventory — single-store full-category capture (`--store-inventory`):**
targets ONE store and runs a complete every-category sweep (the same
catalog-inventory engine `--build-watchlist --catalog` uses), writing a RICH,
COMPLETE record per product into a per-app database in the `inventory/` folder:
`inventory/inventory_blinkit.db`, `inventory/inventory_instamart.db`,
`inventory/inventory_zepto.db`. The table is `inventory_catalog` (columns: `name,
price, mrp, in_stock, url, collections` [the APP's own shelf taxonomy], `category`
[our normalized internal taxonomy from `categorize`], `raw_json` [the full
app-specific payload, opaque — never force-fit into a shared column], plus
`raw_json_truncated`/`raw_json_bytes` for provenance). The operational snapshot
(catalog_snapshots / watchlist / churn) is ALSO written to `deals.db` so Demand
Radar, `--embed-catalog` and the union layer keep working. The capture is
complete (vouchers included); business filtering like voucher exclusion happens
downstream in the union layer, never here.

Requires `--app <app>` + `--store <store_id>`. Location resolves from `--lat/--lon`
(override — lets you target ANY store, even outside this machine's real location),
else from `deals.db.darkstores` for that store (run `--map-locality` first if
unknown). **Run it alone:** starting it while `--demand` / `--map-locality` /
`--build-watchlist` is also crawling the same apps gets the fresh sessions
rate-limited into empty probes
(stores resolve, products = 0). Stop other crawls first (Features panel),
then start the inventory.

**Demand analysis (phase 4):** the dashboard's Demand Radar panels show a
Demand Pressure Index per SKU (Σ OOS-minutes × recency ÷ observation-days),
a hour×SKU stock-out onset heatmap (local time — when demand spikes), and
per-hour delivery-ETA curves. `--demand-report` prints the same in the
terminal; `--csv` exports `exports/dpi_<date>.csv`.

**What DPI actually measures (09-04):** the prober's light sweep samples home
+ 30 category shelves + the store's watchlist search terms — a few hundred
SKUs per cycle — while the watchlist itself is now a full catalog census
(~8-24k rows/store from `--build-watchlist --catalog`). DPI is therefore
shelf-sampler demand pressure, not whole-inventory: stock-outs shorter than
the probe interval are invisible by construction. Delisting truth is
snapshot-driven (`catalog_events` diffs), and the prober only opens `vanished`
events for SKUs it has itself sighted before — a never-sighted row's absence
is a coverage gap, not churn.

**QC-first defaults** (since 08-22): amazon/flipkart tracking is disabled and
the search bot compares quick-commerce platforms only — re-enable via
`adapters.trackers` / `search.platforms` in `config.yaml`.

`--map-locality` resolves each app's distinct darkstores by probing a
landmark+grid anchor set (config → `demand.locality`), enforcing our GPS on
every intercepted API call (cached-location seeding + request rewrite). For
Instamart, each probe also types the anchor's own locality name into the app's
"Add your location" modal (landmark name, rotated landmark names for grid
anchors) so the session binds that anchor's darkstore instead of one shared
city-wide store.
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
    python3 run.py --ui             # dashboard ONLY (no loops); add --bot
                                    # and/or --monitor to also run them alongside
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

    cd ~/Documents/Projects/qcom-scraping
    pip install pyyaml            # optional; a stdlib fallback parser is built in

    # Browser layer uses the PRE-INSTALLED Playwright + Chromium (no install needed):
    #   ../Do not delete folder/node_modules  +  ../Do not delete folder/.pw-browsers
    #   (paths configured in config.yaml → anti_block.node_path / pw_browsers_path)

    cp .env.example .env            # fill TG_BOT_TOKEN / TG_CHAT_ID for phone push
    python3 run.py --check          # validate config + adapters
    python3 run.py --demo           # end-to-end pipeline with an injected glitch
                                    # (writes scratch deals.demo.db — never touches deals.db)
    python3 scripts/live_sweep.py   # hunt REAL glitches across the corridor now
    python3 run.py                  # continuous loop

Without Node/Chromium available the QC adapters degrade gracefully (return no
data) and the trackers + detection engine still run. Demo mode needs nothing
external.

## Config (`config.yaml`)

- `geo.corridor` — Mumbai stations Virar→Andheri with lat/lon. Edit to widen.
- `schedule.crawl_terms` — one rotating search per app per monitor cycle;
  de-biases deal-finder coverage away from the dairy-first home carousel
  ([] = off).
- `anti_block.honey_pot` — canary SKUs with `true_price`. Tune to your basket.
- `detect.*` — glitch thresholds (deviation %, z-score, MRP margin).
- `alert.*` — desktop notification, optional Telegram, log file, rate cap
  (`max_alerts_per_hour` for glitch alerts; `rate_cap` for /watch pushes).
- `demand.*` — Demand Radar: `locality` (name/bbox/landmarks/grid), watchlist
  builder (`categories_per_store`, `staple_queries`, `skip_categories` —
  case-insensitive category-label substrings the deep-sweep queue skips, the
  milk de-biasing knob since apps order their rails dairy-first —,
  `watchlist_max_per_store`),
  prober (`probe_interval_sec`, `probe_terms_max`, `oos_debounce_snapshots`,
  `vanished_cycles`, `stock_canary_queries`, suspect-flip guards).

## Secrets

Never commit `.env`. The old repo accidentally had a Gmail app-password in
`config.json` — this version keeps all secrets in `.env` (git-ignored).

## Output

- macOS notification + optional Telegram push — each shows the deal price
  **with its reference price**: catalog MRP when the app provides one, else the
  "usual" price (median of that item's recent prices at that store, excluding
  the triggering observation).
- `deals.log` — every alert, including `mrp` and `usual` fields.
- `deals.db` — full price history + alerts + the complete search archive
  (`searches` / `search_results` tables; every bot/UI/CLI search lands there,
  tagged with `src/categories.py` product categories for future analysis),
  plus Demand Radar tables (`darkstores`, `watchlist`, `stock_obs`,
  `oos_events`) and Telegram keyword watches (`keyword_watches`).
- `exports/locality_<name>.json` — darkstore map with per-store rotation
  pools of anchor coordinates.

## Live dashboard (`--ui`, http://127.0.0.1:8787)

Bare `--ui` is dashboard-ONLY (starts no loops — start them from Features
below, or launch with `--ui --bot` / `--ui --monitor` to run them alongside).

Real-time view of everything the process does, plus the exit switch:

- **Features** — every capability of this repo as a card (glitch monitor,
  Telegram bot, demand prober, demo, QC probe, inventory sweep, locality
  mapping, watchlist builder, demand report, catalog report, voucher purge).
  Start and stop them right from the browser; each runs as a managed child
  process with live logs, uptime and exit codes. Cards expose their CLI
  arguments as editable fields (apps, store id, `--categories`, `--max-terms`,
  `--catalog` toggle, CSV export, dry-run, …) — fill in what you need and
  ▶ start runs it with those flags; last-used values are remembered in your
  browser. Unknown flags are rejected server-side (the panel only offers
  each feature's own documented flags). STOP ALL / shutdown also reaps
  anything the dashboard started, so nothing keeps crawling in the background.
- **Working area** — change WHERE the tool operates, interactively: drag the
  map pin (or click the map / pick a preset / use your device location / the
  server's IP location) and save it as the Demand Radar locality with a
  radius slider; edit the monitor's corridor stations in a table and set the
  search anchor. Every save is validated against both YAML loaders before it
  lands in config.yaml (previous file backed up in /tmp) and applies when you
  next start or restart a feature. Every darkstore recorded so far is also
  pinned on the map — color/shape-coded per app (▲ blinkit · ◆ zepto ·
  ■ instamart) with per-app counts in a legend and a popup (app, store id,
  label, ETA). Store pins are reference only: clicking one doesn't move the
  working pin, drag/click the map itself as before.
- **AI assistant (optional)** — three helpers on top of live data:
  *Understand the results* explains what the Demand Radar panels show (pressure
  SKUs, temporal patterns, data-quality caveats) — the full analysis is also
  saved as a markdown file (`exports/ai_explain_<stamp>.md`) with a
  **⬇ download full report** link under the panel (served by
  `GET /ai/report/<file>`), so long answers never depend on the dashboard
  box; an **ask a follow-up** field under it lets you keep questioning that
  answer (each Q&A is appended to the same report file, so the download stays
  complete); *Tune probing methodology*
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
  `inventory/inventory_blinkit.db` / `inventory/inventory_instamart.db` /
  `inventory/inventory_zepto.db`):
  pick a database, pick a table, see row counts and the newest rows. The
  same data is available as JSON: `GET /db`, `GET /db/<db>/<table>`.
- **Live feed** — every crawl, cycle, alert, and Telegram query as it happens.
  **Click any row** for details: full ranked results of a search (with codes,
  delivery, links), sample products of a crawl, or an alert's reason/store —
  alert rows carry an inline "was ₹X" chip (MRP, else store-usual) and the
  detail modal breaks out price / MRP / usual with the discount %.
- **Product categories covered** — all crawled products grouped by category.
- **Search history** — every archived search; click to reopen its stored
  results from `deals.db` anytime.
- **Demand Radar panels** — platform health, DPI ranking, onset heatmap,
  per-hour ETA curve.
- **■ STOP ALL** — gracefully ends the dashboard and everything it manages.

## Adapter expansion — 2 new quick-commerce apps (added 08-31, default-OFF)

New adapter modules (`src/adapters/*.py`) for broader tracking:

- `bigbasket` → `BigbasketAdapter` (`name = "bigbasket"`)
- `jiomart` → `JiomartAdapter` (`name = "jiomart"`)

DROPPED the same day, with evidence — do not re-add blindly:

- `dmart`: the DMart web storefront login-gates order/price surfaces and we
  do not create accounts.
- `amazon_now`: Amazon Now is **app-only** (no standalone app either — it's a
  pincoded delivery option inside the main Amazon app, live in Mumbai since
  Sept–Nov 2025 after Bengaluru/Delhi but only in SELECT neighbourhoods, so
  many users never see it). Traced 08-31: the
  `/10-minutes-delivery/s?k=` route is a branded alias over GENERIC amazon.in
  search (query "milk" → Prime Video titles + infant formula, no Now badges),
  `/alm/storefront?almBrandId=ctnow` is the empty legacy Fresh shell (0 cards),
  and the `p_n_alm_brand_id` facet is text-search filtered, not the Now
  assortment. aboutamazon confirms Now is a delivery option in the app.
  Crawling any of these would pollute `price_obs` with non-grocery results.

All follow the same `base.Adapter` pattern (`APP_URL`, `HEALTH_URL`,
`PROBE_TERMS`, `search()`, `crawl()`) and are registered in:

- `config.yaml` (`adapters:` section — **`enabled: false` until crawl-ready**, see below)
- `src/search.py` (adapter maker dict)
- `src/locality.py` (`QC_APPS`)
- `src/orchestrator.py` (`build_adapters()`)
- `src/pricing.py` (`DEFAULT_FEES` — delivery-fee defaults)
- `src/dashboard.py` (`/qc` endpoint loops)

**Route verification** (per workspace rules: `monid` + the `tinyfish` provider —
`XDG_CONFIG_HOME="/Users/Mitesh Gada/Documents/Projects/.monid/xdg" monid run
--provider tinyfish --endpoint /search --query '{"query":"…","domain_type":"web"}'
--wait 90 -j`, free — plus `curl` route probes). Verified shapes:

| App | Home | Search route | Evidence |
|---|---|---|---|
| BigBasket | `bigbasket.com/` (200) | `/ps/?q=<query>` (200; `/search?q=` **403s**) | tinyfish: official site; curl probe |
| JioMart | `jiomart.com/` (200) | `/search?q=<query>` (200) | tinyfish: official site; curl probe |

**Live verdict after deep tracing (08-31, raw-body dumps + wire inspection;
probes ran via `--qc-status --apps …` overrides and direct `pw_catalog.js`
runs from the machine's approximate location):**

| App | Verdict | Root cause (evidence) |
|---|---|---|
| bigbasket | **Akamai-blocked** | `www.bigbasket.com` returns `403 Access Denied` (`errors.edgesuite.net`) to our headless Chromium on **every route and UA** (curl passes, real browsers don't → JS-sensor bot detection). No `bbnow`/alt domain resolves. Not fixable in code from here — needs a residential proxy or real-Chrome fingerprint. |
| jiomart | **extraction works, but IP-locked** | `pw_catalog.js` now harvests **12+ products** (name, price, `price.effective.min`, MRP via `price.marked.min`, `in_stock_variant`, `store_ids[]`, ETA `eta_mins`) from the `ext/vertex/.../products` API. BUT the QC darkstore is resolved **server-side from the request IP**: `delivery-promise` returned the same store at an identical `820.3851 m` across runs whose `app_geolocation` cookie held three different values, and even on the first call before any geo cookie existed. No client-side seed steers it. Search endpoint also rate-limits hard (~4 rapid probes → empty, ~2.5 min recovery). |

So both stay **wired but `enabled: false`**. JioMart's blocker is fundamental to
the repo's "location is enforced, not assumed" invariant — it cannot be driven
to Demand-Radar anchors, so it is at best a machine-location `--store-inventory`
source (and even that is rate-limit-fragile). BigBasket needs an infra change
(proxy/fingerprint) before any of this is revisited. The earlier "needs
location-cache seeding" guess in this section was **wrong** — seeding does not
move either app's store; the blockers are the Akamai edge (bigbasket) and
server-side IP geolocation (jiomart).

DROPPED the same day (see top of section): `dmart` (login wall) and
`amazon_now` (app-only). The jiomart **extraction** fixes in `pw_catalog.js`
are kept regardless — they're correct and serve any future machine-location use.

*Process note:* an earlier revision of this section claimed monid was broken
(`EPERM`) and tinyfish unavailable — both wrong: the EPERM is the documented
`~/.config` sandbox issue fixed by the `XDG_CONFIG_HOME` override above, and
tinyfish is a monid **provider**, not a skill. The built-in `web_search` tool
is banned by the workspace instructions; do not use it here.

## Catalog inventory — full-store snapshots, new & discontinued products (09-02)

`--build-watchlist --catalog` turns the watchlist sweep into a **complete
per-store catalog snapshot**: it visits EVERY category link the app exposes
(one-hop sub-category discovery included — new shelves surface automatically),
skips no category and fires no search terms, archives one
`catalog_snapshots` row per SKU (name/price/stock/categories), and diffs
against the previous snapshot:

- **new** — SKU present now, absent from the previous snapshot (limited-time
  offering candidates; `watchlist.first_seen_ts` marks its first sighting)
- **delisted** — SKU listed in the previous snapshot, absent from the full
  sweep now → logged in `catalog_events`, `watchlist.active=0` (the row is
  kept, so the watchlist doubles as the discontinued-products archive)

Guardrails (AGENTS.md invariants): delisting comes ONLY from full snapshots —
absence from partial sweeps or the prober's light rounds is never churn; a
snapshot whose (name,price) pair count collapses below 70% of the previous
one is recorded but not diffed (mass absence = crawl flake, not churn —
09-04: keyed to pairs and raised 0.5→0.7 after a 56%-pair-count / 24.9%-pair-
overlap Instamart sweep fabricated ~11.5k delistings); vouchers stay
excluded. First snapshot per store is the baseline — no churn events.

Key-rotation noise (09-04): shelves whose payloads omit every product-id
field fall back to a name-slug `sku_key` (see `collect()` in
`tools/pw_catalog.js`), and products re-key slug→id between sweeps. A raw
key diff reads that as churn, so the diff **reconciles on exact (name,price)
pairs**: a "new"/"delisted" key whose pair exists on the other side is a
re-key, not churn — excluded from `catalog_events` and watchlist
deactivation, counted in the run log as `rekeyed (not churn): N`. This
removed ~28% of events on the 09-04 Instamart 1398452 diff; verified by
`python3 -m src.watchlist` (offline self-test).

Inspect churn with `python3 run.py --catalog-report [--store ID]` or the
dashboard's `GET /catalog?store=` endpoint. Cadence is yours — one store per
day via the dashboard **Features** panel or cron works well; a full sweep
runs ~20–60 min per store, so never run it beside `--demand` /
`--map-locality` (rate-limit rule).

## Vouchers are excluded from Demand Radar (09-02)

Digital voucher SKUs (Roblox / Steam / Valorant / Domino's / Amazon Prime /
Blinkit Gift Card / Xbox Game Pass / retail "Instant Voucher" cards, …) are
**not commodities** — their "stock-outs" are gift-code pool replenishments,
not shelf demand. Once the watchlist's unbiased harvest picked up Blinkit's
"E-Gift Cards" shelf, vouchers held 15 of the top-20 DPI slots and skewed
every heatmap/AI analysis.

They are now excluded end-to-end:

- `demand.exclude_vouchers: true` (default) — the watchlist builder drops
  voucher-named SKUs at build time, the prober (`--demand`) skips them in
  every sweep (before observations, events and the soft-block guard), and the
  DPI rollup ignores them defensively.
- The shared matcher is `src/store.py → is_voucher_name()` (name tokens
  "voucher" / "gift card" only — deliberately NOT brand substrings like
  "steam", which would false-positive on garment steamers).
- One-shot cleanup: `python3 run.py --purge-vouchers [--dry-run]` removes
  voucher rows from `watchlist`, `stock_obs` and `oos_events`
  (09-02: purged 1,560 watchlist / 9,689 stock_obs / 78 oos_events rows).
  `--demand` also purges automatically at startup (idempotent no-op once
  clean). Always keep the auto-created `deals.db.bak-*` backup until you have
  verified the result.

The additive schema columns from the retired tracking experiment
(`watchlist.is_digital_voucher`, `stock_obs.voucher_type` /
`restock_trigger`, …) remain per the repo's no-drop schema rule —
`is_digital_voucher` still flags voucher names at upsert as an audit marker;
`voucher_type` / `restock_trigger` are now always NULL. The old design
document `docs/tracking_expansion_vouchers.md` is retired (superseded banner
in-file; kept for the analysis record).
