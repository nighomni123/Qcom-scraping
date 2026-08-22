# Demand Radar — per-darkstore stock-out intelligence

Extends Moneymaker v2 (glitch monitor) with a second mission: **map darkstores in a
locality (e.g. Andheri West), track in/out-of-stock + ETA over time, and derive a
chronological demand-pressure heatmap from stock-out events.**

> Honest framing: order volumes are never exposed by any app. What we measure is
> **stock-out intensity** (frequency × duration × hour-of-day per SKU per store),
> which is a *proxy* for demand. Supply-side gaps can mimic demand spikes; the
> honey-pot-style canaries below help separate the two.

## Status

- **Phase 0 (instrumentation) — DONE, live-verified on Blinkit web.**
  `tools/pw_catalog.js` now captures per-product stock state (`inventory`,
  `is_sold_out`, `in_stock`, … → normalized `in_stock`) and response-level
  metadata (`store_hint`: ranked darkstore-id candidates + `eta_min`).
  Live probe @ Andheri West: 151 products / 151 w/ stock state / eta 8 min.
  Set `DSH_BODY_DIR=<dir>` to dump raw API bodies for field-name discovery.
- **Location enforcement — DONE (was the critical fix).** QC apps cache their
  serving location client-side and ignore our GPS (Blinkit defaulted to a
  Gurugram store). The crawler now seeds `localStorage.location` + `gr_1_*`
  cookies before page scripts run AND rewrites lat/lon in request paths/query/
  JSON bodies onto the target anchor. Verified: visibility resolves Mumbai
  merchants at our exact coords.
- **Phase 1 (locality mapper) — DONE.** `src/locality.py` +
  `python3 run.py --map-locality [--apps blinkit,zepto] [--max-points N]`.
  First sweep found 2 distinct Blinkit darkstores in Andheri West (36517
  station-side, 47578 Kokilaben/Four-Bungalows side); persisted to the
  `darkstores` table + `exports/locality_andheri_west.json`.
  Zepto yields its `storeid` UUIDs even when its WAF blocks the product feed —
  mapping works, feed probing needs phase-2 work.
- **Phase 2 (watchlist builder) — DONE, live-verified on Blinkit store 47578.**
  `src/watchlist.py` + `python3 run.py --build-watchlist [--apps …] [--store …]
  [--max-per-store N] [--max-queries N]`. One browser session per store:
  home harvest → DOM category click-through → staple searches (`--terms`
  queue inside `tools/pw_catalog.js`, collection labels merged on re-sighting).
  Live result: **241 SKUs in ~80 s, all stock-stamped, 2 OOS at build**,
  scored (search hits > home > spread) and capped (60 active / 181 overflow,
  never deleted). Config: `demand.categories_per_store`, `demand.staple_queries`.
- **Phase 3 (stock prober + OOS events) — DONE, live-verified on store 47578.**
  `src/prober.py` + `python3 run.py --demand [--once] [--apps …] [--store …]
  [--max-terms N]`. One browser session per store per cycle re-probes home +
  categories + the watchlist's own search terms; every sighting lands in
  `stock_obs` (null ≠ OOS). Debounced `oos_events` machine is restart-proof
  (streaks rebuilt from recorded observations; `started_at` = first true OOS
  read, not when the threshold was crossed). 'vanished' events after
  `vanished_cycles` missed successful sweeps. Soft-block guards: canary
  queries must return in-stock items and mass OOS flips freeze the event
  machine for that cycle (observations still recorded).
  Live: 3 rounds · 669 obs · event #1 `[oos] sku 547190 started=14:20:46 OPEN`.
- Phases 4–5 (analysis/heatmap, hardening) — designed below, not yet
  implemented. The analyst reads `stock_obs`/`oos_events` directly.

---

## Why the existing architecture fits

| Existing piece | Reuse |
|---|---|
| `tools/pw_catalog.js` browser-intercept | Same trick; extend the JSON walker to keep stock + ETA fields |
| `src/adapters/base.py` | `_browser_catalog` shells out to Node; add `--mode stock` plumbing |
| `src/geo.py` | `Corridor` → becomes `Locality` grid; `resolve_store` → real store discovery |
| `src/orchestrator.py` | jitter/off-peak/stagger scheduling reused for probe loops |
| `src/store.py` | same SQLite; new tables (below) |
| `src/dashboard.py` | add heatmap tab |
| `src/adapters/demo.py` | inject synthetic stock-outs for offline testing |

---

## Phase 0 — Instrumentation (capture what we currently drop)

`collect()` in `tools/pw_catalog.js` and `_parse_catalog()` in `adapters/base.py`
keep only `name/price/mrp`. Extend both walkers to also lift, per product node:

- stock candidates: `in_stock`, `instock`, `is_available`, `available`,
  `availability`, `out_of_stock`, `oos`, `stock`, `sold_out`
- ETA candidates (store/page level): `eta`, `delivery_time`, `delivery_eta`,
  `eta_minutes`, `sla`, `display_eta`
- scarcity badges where present: `only_few_left`, `fast_selling`

Field names rotate per app/build — same philosophy as prices: **capture
generically, normalize downstream.** Normalized product dict gains:

```
{ sku_key, name, price, mrp, url,
  in_stock: true|false|null,      # null = unknown (don't invent)
  badges: ["few_left", ...] }
```

Store-level response metadata gains `eta_min` when present.

Acceptance: `node tools/pw_catalog.js --app blinkit --url https://blinkit.com/ ...`
returns products whose `in_stock` is populated (spot-check 3 against the live app).

## Phase 1 — Locality mapper (`src/locality.py`, new)

1. **Anchor grid**: generate N×N points over the locality bbox (Andheri West:
   ~19.103–19.160 N, 72.820–72.855 E, ~400–600 m spacing) plus named landmark
   seeds (Versova, Yari Road, Lokhandwala, Four/Seven Bungalows, DN Nagar,
   Andheri W station, Infiniti Mall, Kokilaben).
2. **Discovery pass**: for each point × each QC app, resolve the serving
   darkstore (intercepted home/location call). Persist to new table:

```sql
CREATE TABLE IF NOT EXISTS darkstores (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL, app TEXT, store_id TEXT, label TEXT,
  lat REAL, lon REAL,          -- representative anchor
  eta_min REAL                 -- last seen delivery estimate
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_store ON darkstores(app, store_id);
```

3. **Clustering**: group anchor points by resolved `store_id`. Points sharing a
   store = its catchment outline. Output: per-app list of distinct Andheri-West
   darkstores, each with a rotation pool of 3–5 anchor coords.

**Address randomisation rule (changed from original idea):** probe each
*(app, store)* once per cycle; rotate the representative anchor across cycles.
Same-store multi-point probing in one cycle yields duplicate data and doubles
the bot fingerprint for zero information.

## Phase 2 — Watchlist builder (`scripts/build_watchlist.py`, new)

- Deep sweep per discovered store (category collections, not infinite scroll):
  persist every SKU seen with its latest stock state.
- Table:

```sql
CREATE TABLE IF NOT EXISTS watchlist (
  app TEXT, store_id TEXT, sku_key TEXT,
  name TEXT, collection TEXT,   -- which collection page surfaces it
  active INTEGER DEFAULT 1,
  PRIMARY KEY (app, store_id, sku_key)
);
```

- Curate down to a probe set (target 150–400 SKUs per store): top categories +
  high-velocity staples + user additions. Collections are the probe unit —
  one collection fetch returns dozens of SKUs *with stock flags*, so cost per
  SKU is tiny compared to per-SKU search calls.

## Phase 3 — Stock prober (`src/prober.py` + orchestrator mode, new)

Loop per *(app, store)*: fetch its watchlist collections → normalized rows →

```sql
CREATE TABLE IF NOT EXISTS stock_obs (
  ts REAL, app TEXT, store_id TEXT, sku_key TEXT,
  in_stock INTEGER,        -- 1/0/null
  price REAL, mrp REAL, eta_min REAL,
  source TEXT              -- 'collection' | 'search' | 'dom'
);
CREATE INDEX IF NOT EXISTS idx_so ON stock_obs(store_id, sku_key, ts);

CREATE TABLE IF NOT EXISTS oos_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  app TEXT, store_id TEXT, sku_key TEXT,
  started_at REAL, ended_at REAL,       -- ended_at NULL = ongoing
  snapshots INTEGER                     -- consecutive OOS obs (debounce)
);
```

State machine per (store, sku): IN → OOS on first `in_stock=0`;
OOS → IN closes the event. **Debounce rules:**
- A crawl failure / empty parse is `null`, never `0` — never log an OOS from a
  scraper error.
- Require ≥2 consecutive OOS snapshots OR a prior sighting of the SKU in the
  same session, to kill flaky one-offs.
- Delisting (SKU absent from its collection ≥K cycles) logged as
  `ended_at=NULL, reason='vanished'` — distinguishable from clean restock.

**Cadence:** 12–15 min per store peak hours; off-peak speedup (existing
`offpeak_speedup`) tightens night cycles. Snapshot interval = temporal
resolution of the heatmap; sub-interval stock-outs shorter than one cycle are
invisible — accepted trade-off, documented in output.

**Stock canaries** (mirror of price honey-pot): 3–5 SKUs per app that are
*effectively always in stock* (e.g. Amul Taaza 500ml). Unexpected OOS on a
canary ⇒ suspect scraper artifact / soft-block, raise health flag, slow down.

## Phase 4 — Analysis + heatmap (`src/demand.py` + dashboard tab)

Rollups (SQL views or Python):

- **Demand Pressure Index** per (store, sku, day):
  `DPI = Σ(oos_events.duration_min × recency_weight) / observation_window`
  optionally × velocity guess (restock speed: shorter OOS before restock =
  faster seller).
- **Chronological heatmap**: hour-of-day × SKU matrix of OOS onset counts,
  aggregated per store and per locality; 24 columns, SKU rows sorted by DPI.
- **ETA curve** per store: mean/min/max ETA by hour (rider-supply signal).
- Outputs: dashboard heatmap tab (`ui.port`), nightly CSV export
  `exports/dpi_<date>.csv`, and a Telegram digest (optional, reuse `alert.py`).

## Phase 5 — Hardening

- Proxy/IP↔coords coherence: prefer Mumbai-exit residential proxies
  (`PROXY_URL`) so spoofed GPS matches IP geo; else expect degraded store
  resolution from far IPs.
- Per-store stagger + jitter (reuse scheduler); global cap on requests/hour;
  exponential backoff on WAF/challenge hits (existing `awswaf` reload logic).
- Identity rotation stays as-is (UA/install-id per cycle).

---

## Config addition (`config.yaml`)

```yaml
demand:
  enabled: false
  locality:
    name: "Andheri West"
    bbox: { min_lat: 19.103, max_lat: 19.160, min_lon: 72.820, max_lon: 72.855 }
    grid_step_m: 500
    landmarks: [Versova, Yari Road, Lokhandwala, Four Bungalows, Seven Bungalows,
                DN Nagar, "Andheri West station", "Infiniti Mall"]
  probe_interval_sec: 900       # per store; jittered
  watchlist_max_per_store: 300
  oos_debounce_snapshots: 2
  vanished_cycles: 4
```

## Run modes

```
python3 run.py --map-locality            # Phase 1 discovery → darkstores table
python3 run.py --build-watchlist         # Phase 2
python3 run.py --demand                  # Phases 3–4 loop (+ dashboard)
python3 run.py --demand --demo           # injected synthetic stock-outs, offline
```

## Known limits (stated up front)

1. Demand numbers are **proxies**, not sales figures.
2. Temporal resolution bounded by probe cadence.
3. Delisting masquerades as long OOS unless the vanished-state rule is applied.
4. Store resolution quality depends on IP/GPS coherence; expect occasional
   wrong-store attribution from non-Mumbai IPs.
5. Scraping breaches the apps' ToS; keep volume modest, research-only, no fake
   accounts/orders, don't resell the data.
