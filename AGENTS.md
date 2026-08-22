# AGENTS.md — Moneymaker

Notes for humans and agents working on this repo. Read this before changing
anything; it covers the non-obvious environment quirks and the invariants that
keep the data honest.

## What this repo is

One tool, three features (all sharing the same browser-intercept crawler):

1. **Glitch monitor** — price anomalies across Blinkit/Zepto/Instamart on the
   Mumbai corridor (Virar→Andheri). Design: `ARCHITECTURE.md`.
2. **Price-search Telegram bot** — cheapest offer per product across 5
   platforms (`src/search.py`, `src/tgbot.py`, `src/pricing.py`), with
   search-history persistence + keyword category analytics
   (`src/categories.py`; dashboard endpoints `/searches`, `/categories`).
3. **Demand Radar** — per-darkstore stock-out intelligence for a locality
   (Andheri West first). Design + status: `DEMAND_RADAR.md`. Pipeline:
   `--map-locality` → `--build-watchlist` → `--demand` → (phase 4: analysis).

## Concurrent agents — coordination rules

This repo is edited by MORE THAN ONE agent at a time (as of 08-22: one on the
Demand Radar phases, one on search-history/category analytics). Rules:

- **Verify before AND after you edit**: run the full check suite (below) on
  entry — if it fails, someone else is mid-edit; wait and re-read the file.
- **Targeted edits only.** Never rewrite whole files from memory; re-read the
  current content first (another agent may have just changed it). Use small
  anchored `old_string` replacements, never full-file overwrites.
- **Schema changes are additive**: `CREATE TABLE IF NOT EXISTS` / new columns
  via backfill (see `Store._backfill_categories` for the pattern). Never
  drop/rename tables or columns — other features read them live.
- **config.yaml must stay miniyaml-compatible** (stdlib fallback parser when
  PyYAML is absent). Add new sections at the END of an existing section with
  comments; don't reorder keys.
- **Document as you go**: new CLI flags → README + the Commands list here;
  new modules/tables → Repo map; new behavioral invariants → the invariants
  section. Undocumented behavior will get broken by the next agent.
- Don't run two long crawls (`--demand`, `--map-locality`, `--build-watchlist`,
  `live_sweep.py`) simultaneously against the same app — rate-limit bans hurt
  both workstreams. Coordinate timing instead.

## Repo map

```
run.py                  entrypoint; ALL CLI flags live here (--check --demo
                        --search --bot --ui --once --map-locality
                        --build-watchlist --demand; flag args parsed by
                        _flag_* helpers)
config.yaml             every tunable; secrets go in .env only
codes.yaml              delivery fees + offer codes (user-editable)
tools/pw_catalog.js     THE crawler: real app in headless Chromium, intercepts
                        signed API calls; deep-sweep visit queue (categories +
                        search terms); location seeding + request rewrite;
                        DSH_BODY_DIR raw-body dump hook
src/
  orchestrator.py       glitch-monitor loop (jitter, off-peak speedup)
  locality.py           phase 1: anchor grid + darkstore discovery/clustering
  watchlist.py          phase 2: per-store SKU probe set builder
  prober.py             phase 3: stock_obs loop + debounced oos_events machine
  categories.py         keyword product-category classifier (ordered rules)
  store.py              sqlite schema + all persistence helpers
  geo.py                corridor anchors + store resolver (glitch monitor)
  adapters/             blinkit / zepto / instamart / amazon / flipkart /
                        trackers / demo; base.py holds the browser machinery
  detect.py alert.py honey.py events.py dashboard.py search.py pricing.py
                        tgbot.py miniyaml.py (stdlib YAML fallback)
exports/                locality mapping JSON (rotation pools per store)
scripts/live_sweep.py   one-shot real-glitch hunt
deals.db                everything: price_obs, alerts, darkstores, watchlist,
                        stock_obs, oos_events, searches, search_results
```

## Commands

    python3 run.py --check                 # config + adapters sanity
    python3 run.py --demo                  # offline pipeline test (injected glitch)
    python3 run.py --map-locality [--apps blinkit,zepto] [--max-points N]
    python3 run.py --build-watchlist [--apps …] [--store ID]
                                    [--max-per-store N] [--max-queries N]
    python3 run.py --demand [--once] [--apps …] [--store ID] [--max-terms N]

"Test suite" = `python3 -m py_compile` on touched files + `node --check
tools/pw_catalog.js` + `python3 run.py --check`. `src/prober.py` and
`src/locality.py` have offline self-tests (`python3 -m src.prober`,
`python3 -m src.locality`). Live smokes: `--demand --once --max-terms 5`
(~70 s), `--map-locality --max-points 2` (~2 min).

## Environment quirks (important)

- **Playwright is pre-installed elsewhere**: `../Do not delete folder/`
  (`node_modules` + `.pw-browsers`), wired via `config.yaml → anti_block.*`.
  The JS helper needs `NODE_PATH` + `PLAYWRIGHT_BROWSERS_PATH` env — the
  Python adapter sets them from config automatically. Don't `npm install`.
- **PyYAML is optional.** If missing, `run.py` falls back to `src/miniyaml.py`
  (a subset parser). Keep `config.yaml` shapes within what it parses (the
  existing file is the compatibility spec).
- **Node 22 at /usr/local/bin/node.** After editing `tools/pw_catalog.js`,
  always `node --check`. The Python side reads only the LAST stdout line as
  JSON — anything else must go to **stderr** (`console.error`).
- **Field-name discovery**: set `DSH_BODY_DIR=/tmp/bodies` when running the
  helper directly to dump every intercepted JSON body. QC apps rotate their
  payload field names; when stock/ETA/store extraction degrades, re-discover
  from dumps instead of guessing.
- **Location is enforced, not assumed**: QC apps cache their serving location
  client-side and will silently serve a DIFFERENT CITY's store (Blinkit
  defaulted to Gurugram). `pw_catalog.js` seeds `localStorage.location`,
  `gr_1_lat/lon` cookies, drops the cached `merchant`, AND rewrites lat/lon in
  request paths/query/JSON bodies onto the target anchor. If you add a new app,
  replicate both layers. Verify via the `visibility`-style response (city must
  match the anchor).

## Demand Radar invariants (do not break)

- **NULL ≠ OOS.** A failed parse/crawl records `in_stock=NULL`; it must never
  become 0. Scraper errors must not fabricate demand.
- **Debounce.** An `oos` event opens only after `demand.oos_debounce_snapshots`
  consecutive zero-reads; any 1-read closes it; nulls pause, not reset.
- **Streaks are restart-proof** — rebuilt from `stock_obs` history
  (`Store.trailing_oos_streak`) each sweep; `started_at` is the FIRST zero
  read, never the moment the threshold was crossed.
- **Vanished ≠ OOS.** An active watchlist SKU missing from `vanished_cycles`
  successful sweeps opens `kind='vanished'`, not `kind='oos'`.
- **Suspect cycles freeze the machine.** Canary queries returning only-OOS or
  a mass in-stock→OOS flip ⇒ record observations, open/close nothing.
- **One browser session per store per sweep.** Home → categories → searches
  inside a single page; never spawn per-SKU or per-term processes.
- **Stock/ETA are per darkstore, not per address.** Anchor grids discover
  catchments; probes hit each store once per cycle (rotate anchors across
  cycles later if needed).
- ETA is per-store/per-snapshot (`stock_obs.eta_min`) — there is no
  per-product delivery time in any app.

## Known limitations / TODO

- Zepto: WAF blocks most feed traffic → store-id mapping works, product-level
  probing needs work (search-based fallback is the likely path).
- Instamart: adapter exists; not yet live-verified for demand phases.
- Phase 4 (analysis/heatmap/dashboard) and phase 5 (hardening: proxy/IP
  coherence, per-store anchor rotation) not yet built — see DEMAND_RADAR.md.
- Demand numbers are a stock-out-intensity PROXY for demand, never order
  volumes. Keep volumes modest; research only; no fake accounts/orders.
