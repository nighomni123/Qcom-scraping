# qcom-scraping v2 — Architecture

Updated 2025-09-08 to reflect current repo state (evidence from `AGENTS.md`, `src/`, `run.py`, `config.yaml`, `deals.db` schema, and `tools/pw_catalog.js`).

> **Interactive architecture diagram (archify skill):** authored spec saved to `/tmp/qcom_arch.json` (showcase profile, 13 components covering adapters / browser intercept / geo-store / database / demand pipeline / search / product-space / embeddings / dashboard). The HTML render requires geometry adjustments (label overlap); the spec is the authoritative topology reference.

---

## 1. What this repo is

One browser-intercept crawler (`tools/pw_catalog.js`) shared by three live features:

1. **Glitch monitor** — per-darkstore price anomalies across the Mumbai corridor (Virar → Andheri).
2. **Price-search Telegram bot** (`--bot`) — cheapest effective price across 7 tracked apps with `/watch` keyword pushes (`WatchPusher`) and `/digest` daily summary.
3. **Demand Radar** (`--map-locality` → `--build-watchlist` → `--demand`) — per-store stock-out intelligence; phase 4 (DPI / heatmap / ETA) is **BUILT** (verified 08-22); phase 5 (proxy plumbing) is **WIRED end-to-end** but live validation with a Mumbai residential pool is still **PENDING**.

Plus the M1–M7 **Product-Space Intelligence** pipeline (`--product-space`, `--product-vectors`, `--detect-assortment-gaps`, `--detect-gaps`, `--score-opportunities --persist`, `--opportunity-report`, `--temporal-analysis`, `--validate-opportunities`, `--opportunity-pipeline`) and the optional **semantic-embedding** layer (`--embed-catalog`, `--similar` — NVIDIA `nvidia/llama-nemotron-embed-vl-1b-v2` @2048 dims + opt-in local Ollama `embeddinggemma-300m` @768 dims).

---

## 2. Core design invariants

- **The app is the API documentation.** We never guess endpoints. `pw_catalog.js` runs the real mobile web app in headless Chromium, intercepts its signed XHR/fetch calls, and replays them exactly (`request-signature`, `x-csrf-secret`, `x-api-key`, `device_id`, `session_id`). When paths rotate, the next page load rediscovers them.
- **Location is enforced, not assumed.** Each adapter seeds `localStorage.location`, rewrites `lat`/`lon` in cookies/requests, and drops cached `merchant` IDs. Apps that cache serving store server-side from the request IP (e.g. Jiomart) must match exit-IP to anchor geo; this is the acceptance test for Phase 5 proxies.
- **Vouchers are excluded from every demand surface.** `is_voucher_name()` (token match `voucher` / `gift card`) is used by watchlist builder, prober, DPI rollups, and purge (`--purge-vouchers`). Their "stock-outs" are code-pool replenishments, not demand.
- **Schema changes are additive only.** `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE ADD COLUMN` with backfills (`_backfill_categories`, `_backfill_first_seen`). No drops/renames — other features read `deals.db` live.
- **One browser session per store per sweep.** The continuous loop (`--demand`) never spawns per-SKU processes. Catalog mode (`--catalog`) is the exception: it runs ONE full sweep per store and writes `catalog_snapshots` + `catalog_events` (`new`/`delisted`) via pair reconciliation (`name,price`). A snapshot whose pair count collapses to <70% of previous is recorded but its diff is **skipped** (mass-absence = crawl flake, not churn).
- **NULL ≠ OOS.** A failed parse/crawl writes `in_stock=NULL`; it must never become `0`. Scraper errors must not fabricate demand.
- **Delisting is snapshot-driven only (`catalog_events`).** A partial sweep or light prob round never produces `delisted`. The watchlist's `active=0` rows are kept (the discontinued archive); `oos_events` for `kind='oos'` are reconciled with `catalog_events`, not the reverse.
- **Restart-proof streaks.** `Store.trailing_oos_streak()` rebuilds from `stock_obs` history; `started_at` is the first zero read, never the threshold-crossing time.
- **Vanished ≠ OOS.** A SKU missing from successful sweeps but with prior `stock_obs` sightings opens `kind='vanished'` (not `oos`). A never-sighted SKU (catalog-built but never probed) is a coverage gap, not churn.
- **Suspect cycles freeze the machine.** Canary queries returning only-OOS or mass in-stock→OOS flip record observations but open/close nothing.
- **Silent-hour coverage matters.** `demand.sweep_windows` (local hours) gates loop starts; the Aug 24–Sep 1 history had zero observations in hours 02–10 and 15–16, so onset windows in those hours are biased. Keep `--demand` running 24/7 or set windows to fill them.

---

## 3. Adapter layer (`src/adapters/`)

All 7 adapters subclass `base.Adapter`. Each defines `APP_URL`, `HEALTH_URL` (override for apps whose home serves zero products — Zepto), `PROBE_URL`, `PROBE_TERMS`, and `name`. The adapter interface:

- `crawl(station, lat, lon)` → list of `{sku_key, name, price, mrp, url, in_stock}`.
- `search(query, lat, lon)` → search results (token overlap ≥ 0.5; optional semantic blend from `embeddings` table).
- `probe_point(station, lat, lon, im_term)` → single product-bearing probe; `PROBE_TERMS` fired in-session for catalog breadth beyond home carousel.
- `deep_sweep(station, lat, lon, categories, terms, deep_cats, skip, mirror_page_ms, tabs, on_batch)` → full watchlist sweep.

**Enabled apps and live state:**

| App | Adapter | Key behavior / fix date |
|-----|---------|------------------------|
| **Blinkit** (`blinkit.py`) | `blinkit` | `APP_URL` = category shelf routes (`/dc/`); text search (`/s/?q=`) serves **no tobacco** (server-side curated) — tobacco SKUs come from `--pre` shelf visit (`URL::LABEL`) merged into search results (`BlinkitAdapter.search`). Category regex fixed 09-02 (`(cn|category|categories|c|dc|sc)(/|$)`) — `dc/` routes yield ~300 products per shelf in warm sessions; old regex collapsed to home feed (~104 SKUs, all `collections=home`). Age gate is UI-only; crawler goes straight to shelf URLs. |
| **Zepto** (`zepto.py`) | `zepto` | `HEALTH_URL` overrides to search route (home serves no products for fresh sessions). Signing (`request-signature`, `x-csrf-secret`, `x-xsrf-token`, `x-api-key`, `device_id`, `session_id`) lives in web bundle chunks (`89411-*.js`, `88682-*.js`); crawler harvests already-signed responses — no forging needed. Hard gate is IP-reputation (403 WAF), not signature validation. Fixed 08-22. |
| **Instamart** (`instamart.py`) | `instamart` | Target is `instamart.in` (NOT `www.swiggy.com/instamart` — login-walled for our IP). Inner-scrollable container pagination (`POST /api/instamart/category-listing/filter/v2`) with advancing `items_offset` (stride 20); URL `pageNo`/`offset` are decoys. Mirror pagination (`--mirror-stride 20 --mirror-max-pages 80 --mirror-page-ms 900`) is default; `--scroll-rounds` is opt-in. `--im-term` passes locality term to modal binding. Subcategory routes (`/sc/<l0>/<l1>-<id>/t10`) matched by regex (`sc` added 09-02). |
| **Amazon** (`amazon.py`) | `amazon` | `amazon.in` search (DOM extraction). |
| **Flipkart** (`flipkart.py`) | `flipkart` | Search + price-track (`trackers.py`). |
| **Jiomart** (`jiomart.py`) | `jiomart` | **Enabled**, but block: store is resolved **server-side from request IP** (delivery promise identical at 820.3851 m across different `app_geolocation` cookie values). Client-side location seeding does nothing. Rate-limits hard (~4 rapid probes → empty; ~2.5 min recovery). Not anchor-steerable; at best a machine-location `--store-inventory` source. |
| **Bigbasket** (`bigbasket.py`) | `bigbasket` | `enabled: false`. Akamai Bot Manager 403 (`errors.edgesuite.net`) on every route + UA; curl passes, browsers don't. No `bbnow` domain resolves. Revisit requires residential proxy or real-Chrome fingerprint. |

**Adapter expansion notes (dropped/rejected):**
- `dmart` — dropped 08-31: web is login-gated; no fake accounts.
- `amazon_now` — dropped 08-31: Amazon Now IS live in Mumbai (expanded Sept–Nov 2025) but only in SELECT neighbourhoods; no standalone app; web routes serve generic search or empty Fresh shell. Would pollute `price_obs`.
- `demo` adapter (`demo.py`) — simulated source with injected glitch (`--demo`); runs against scratch `deals.demo.db` (NEVER touches real `deals.db`).

---

## 4. Browser-intercept layer (`tools/pw_catalog.js`)

- **Engine:** Playwright (pre-installed at `../Do not delete folder/node_modules` + `.pw-browsers`; wired via `config.yaml` `anti_block.node_path` / `anti_block.pw_browsers_path`).
- **Anti-block machinery (`base.py`):**
  - `MOBILE_UAS` — 4 mobile UAs (iPhone / Android / Pixel); `rotate_user_agent` toggle.
  - `fresh_install_id()` — 16-char plausible device ID.
  - `load_proxies()` / `pick_proxy()` — reads `PROXY_URL` env + `config.yaml` `anti_block.proxies`.
  - `_rotate()` returns `(ua, iid, proxy)`.
- **Geo seed (`pw_catalog.js` init):** seeds `localStorage.location`, rewrites `lat`/`lon` in cookies/request paths/query/body, drops cached `merchant`.
- **Deep-cats discovery (`--deep-cats`):** one-hop BFS (`sc` / `category` links) for sub-category shelf links. Catalog inventory mode (`--catalog`) uses this; `skip_categories` overrides to `[]` (no skips — a skipped shelf would fabricate delistings).
- **Mirror pagination (`--mirror-page-ms`, `--mirror-stride`, `--mirror-max-pages`, `--tabs`):** mirrors the visit's own captured listing POST (`apiHits` mirror pattern — session's own auth, no forging). `items_offset` advances (20 stride). `--tabs N` opens N concurrent tabs inside the SAME browser context (same `device_id`, one session/store).
- **Progress streaming (`anti_block.stream_progress`):** `stderr` lines (`[app_label] ...`) stream live; stdout carries batch JSON (`{"type":"batch","products":...}`) + terminal summary JSON (`{"products":...,"store_hint":...,"api_endpoints_seen":...}`). `on_batch` writes incrementally to sqlite.
- **Timeout / reap:** process group (`start_new_session`); timer kills group on timeout; reap watchdog kills grandchildren if process exits but browsers hold pipes.
- **Multi-tab (`--tabs N`):** intra-session concurrency (same `device_id`, same visit queue, each tab keeps own intercepted-api list + collection label). Cross-tab throttling possible; `--tabs 3 --mirror-page-ms 600` tested on Instamart.

---

## 5. Demand Radar pipeline (phases 1–4 built; phase 5 wired)

Evidence: `DEMAND_RADAR.md`, `AGENTS.md`, `src/locality.py`, `src/watchlist.py`, `src/prober.py`, `src/demand.py`, `run.py` flags (`--map-locality`, `--build-watchlist`, `--demand`, `--demand-report`, `--catalog-report`, `--store-inventory`, `--catalog`, `--catalog`, `--purge-vouchers`, `--qc-status`).

### Phase 1: Locality (`locality.py`)
- `anchor` grid + `landmark` seeds → resolve + cluster darkstores.
- `geo.corridor` regenerated by location editor (`/location` POST); validated with BOTH `miniyaml` loaders before atomic write; previous file backed to `/tmp`.
- `exports/locality_<name>.json` rotation pools per store.

### Phase 2: Watchlist (`watchlist.py`)
- Per-store SKU probe set: `home` harvest → DOM category click-through (`categories=...`) + staple searches (`terms=...`).
- Scored by search hits > home; `score DESC` orders active set.
- `catalog` mode (`--catalog`): ALL category links (`--deep-cats`), NO search terms, NO `skip_categories` (skipped shelf = false delisting). Writes `catalog_snapshots` + diff events (`new`/`delisted`) vs previous snapshot (pair reconciliation on exact `(name,price)`).
- `deactivate_watchlist_except()` keeps inactive rows (discontinued archive); never deletes.

### Phase 3: Prober (`prober.py`)
- Continuous loop (`--demand`): re-sweep active watchlist collections; writes `stock_obs` snapshots; runs debounced `oos_events` machine.
- `open_oos_event()` opens only when `snapshots` consecutive zero reads met; any `1` closes it immediately. Nulls (`in_stock=NULL`) pause, not reset.
- `vanished` (`kind='vanished'`) opens only for SKUs with prior `stock_obs` sightings. Catalog-covered never-sighted rows are coverage gaps, not churn.
- `close_oos_event()` reconciles stale `oos` events for SKUs outside active set at last observed reading (durations never fabricate unseen time).
- Suspect-cycle freeze: only-OOS canary results or mass in-stock→OOS flip freeze machine (record observations, no events).
- Voucher purge (`store.purge_vouchers()`): defensive removal from `watchlist`, `stock_obs`, `oos_events`; idempotent.

### Phase 4: Analysis (`demand.py` — BUILT 08-22)
- `dpi_table()` — ranked `[{sku_key, name, dpi, n_events, total_min, mean_restock_min, last_price, last_seen, active}]`; excludes `is_voucher_name()`.
- `heatmap()` — hour-of-day (local) × top-DPI SKU matrix of `oos` onset counts.
- `eta_curve()` — per-hour ETA stats from `stock_obs.eta_min`.
- `demand_summary()` — per-darkstore overview + totals.
- `export_csv()` → `exports/dpi_<date>.csv`.
- `run.py --demand-report [--store ID] [--csv]` — DPI table + heatmap summary.
- `run.py --catalog-report [--store ID]` — catalog snapshots + new/delisted churn log.

### Phase 5: Hardening (WIRED; PENDING live validation)
- `anti_block.proxies` (config) + `PROXY_URL` env → `load_proxies()` + `pick_proxy()` → `--proxy` → `pw_catalog.js` `browser.newContext({proxy})`.
- One proxy per invocation = one fresh browser+context = fresh WAF identity (session-bound token constraint; never rotate under live context).
- Empty pool = exact old behavior (verified: blinkit probe unchanged).
- Acceptance test: jiomart store MUST change per exit IP (IP-locked resolution).
- Global launcher `.tools/ollama/ollama-global` wraps binary, pins `HOME`/`OLLAMA_HOME`/`OLLAMA_MODELS` into workspace; symlinked `/usr/local/bin/ollama`.

---

## 6. Search, bot, and pricing (`search.py`, `tgbot.py`, `pricing.py`, `categories.py`)

- **Cross-platform fan-out:** `search.py` launches all 5 tracked apps in parallel (`ThreadPool`); results ranked by `effective` price (`pricing.py`: listed price − best `codes.yaml` offer − delivery fee).
- **Matching:** `token overlap ≥ 0.5` (`fuzzy`); optional semantic lift via `embeddings` table (`blend = max(token, semantic)` — adds candidates, never demotes; endpoint down / `semantic_matching: false` → exact historical token behavior).
- **Telegram bot (`tgbot.py`):**
  - `/watch <product>` → `keyword_watches` (chat-level); `/unwatch <product>` deactivates (history preserved).
  - `/digest` → today's cheapest per category (`store.today_cheapest_by_category`) + Demand Radar DPI top-5 (`demand.dpi_table`).
  - `WatchPusher` pushes when crawl/search results match active watches; rate-capped (`alert.rate_cap` pushes/hour + 6h per-watch cooldown via `last_alerted_ts`); restart-proof.
- **Categories (`categories.py`):** keyword classifier; ordered rules; feeds `category` column and `categories` stats.

---

## 7. Product-Space Intelligence pipeline (`src/product_space.py`, `src/product_vectors.py`, etc.)

Evidence: `PRODUCT_SPACE_PLAN.md`, `IMPLEMENTATION_LOG.md`, M1–M7 references.

- **M1 (`product_space.py`):** `load_product_space()` — reads `inventory/inventory_<app>.db.inventory_catalog` UNION `deals.db.catalog_snapshots` (latest per SKU fallback); excludes vouchers; attaches parsed fields (`product_fields.parse_name`); runs entity-resolution (exact canonical key → within-brand `match_score` → pack/variant veto → semantic confirm fallback; semantics never override veto). Provenance: every row carries `app/store_id/sku_key/inventory_db`.
- **M2 (`product_vectors.py`):** attribute + commercial vectors per product group (price/density normalized; `demand.dpi_table` mean; store/app coverage; churn flags). `--csv` → `exports/product_vectors.csv`.
- **M3 (`detect_assortment_gaps` / `assortment_gaps.py`):** cross-app gaps (present on some apps, not all).
- **M4 (`density.py` / `product_density`):** per-category density + insufficient / stale coverage guards.
- **M5 (`detect_gaps` / `gaps.py`):** internal/attribute gap scoring on top of density guards.
- **M6 (`opportunities.py` / `score_opportunities`):** `gap_strength × DPI × coverage × churn` scoring with provenance + validation codes; `--persist` writes to `deals.db.opportunities`.
- **M7 (`opportunity_report` / `opportunities.py`):** `run.py --opportunity-report [--limit N]` — latest persisted snapshot.

---

## 8. Semantic embeddings (`src/embed.py`, `.tools/ollama/`)

- **NVIDIA (default):** `nvidia/llama-nemotron-embed-vl-1b-v2` @2048 dims; `ai.embedding_base_url` + `NVIDIA_Build_API_KEY`; asymmetric (`corpus=passage`, `query=query`); batch 1000; 50 req/day free tier (`X-RateLimit-Limit`). Cache: additive `embeddings` table (keyed `model+dims`); backfill journaled `logs/embed_backfill.log`.
- **Local Ollama (opt-in):** `ai.embedding_provider: "ollama"`; `.tools/ollama/ollama-global` binary; `embeddinggemma-300m` @768 dims; server HOME `.ollama_home/`; `embedding_batch: 256` is optimum (throughput flat ~8 items/s; B≥512 kills runner). `.ollama/` models ~621MB total.
- **Backfill commands:** `--embed-catalog [--limit N]` (NVIDIA); `--similar <phrase> [--limit N]` (offline lexical + stored-vector expansion); `scripts/test_embeddinggemma.py` (self-managing local backfill + demo queries); `scripts/compare_embeddings.py` (gemma vs NVIDIA agreement); `scripts/embed_3d_map.py` (semantic explorer → `exports/embedding_map.html` / `embedding_map_gemma.html`).
- **Catalog re-key guard (rejected 09-05):** brand-sibling names (`Coke Zero` / `Coke`) cosine above genuine relabels; semantic catalog re-key rejected per `AGENTS.md`. Watchlist key-rotation handled via exact `(name,price)` pair reconciliation, not embeddings.

---

## 9. Dashboard (`run.py --ui` → `http://127.0.0.1:8787`)

Evidence: `README.md`, `AGENTS.md` endpoints list.

- **Features panel (`/features`):** spawn/terminate any repo feature (`monitor`, `bot`, `--demand`, `demo`, `inventory sweeps`) as managed child processes. Editable `args` spec per feature; server whitelists flags against spec (unknown flags rejected 400, never forwarded). `localStorage` persists last-used values. `STOP ALL /shutdown` reaps all children.
- **Endpoints:**
  - `/status` — live event bus (`fkind="gate" | "skip" | "boost"` for WAF failover).
  - `/categories`, `/searches`, `/search/<id>` — bot/search analytics.
  - `/demand`, `/heatmap?store=`, `/eta`, `/qc` — DB-backed Demand Radar.
  - `/db`, `/db/<db>/<table>` — read-only browser over all sqlite files (`deals.db`, `inventory/*.db`).
  - `/semsearch?q=` — lexical search; `/semsearch?seed=` — stored-vector expansion (`gemma` / `google` / `nematron` silos).
  - `/location`, `/location/presets`, `/location/locality`, `/location/corridor` — interactive working-area editor (regenerates `geo.corridor`, `demand.locality`, `search.station` in `config.yaml` with both loaders validated before atomic write).
  - `/ai/status`, `/ai/explain`, `/ai/explain/followup`, `/ai/methodology`, `/ai/focus` — optional LLM assistant (`src/ai_assist.py`); provider = any OpenAI-compatible endpoint (`ai:` in `config.yaml`) or local Ollama (keyless); `/ai/explain` persists full analysis as markdown to `exports/ai_explain_<stamp>.md`; `/ai/explain/followup` appends Q&A to saved file; `/ai/methodology[/apply]` and `/ai/focus[/apply]` enforce suggestions against `INT_PARAMS` whitelist (only `demand:` knobs writable). No arbitrary CLI execution.

---

## 10. Data model (`deals.db` + `inventory/*.db`)

Evidence: `src/store.py` schema (`_SCHEMA`), `AGENTS.md` repo map.

### `deals.db` (main sqlite; WAL mode)

| Table | Purpose | Key invariants |
|-------|---------|----------------|
| `price_obs` | Price history per `(app, store_id, sku_key)` | Rolling window (`usual_price()` excludes newest row; used for alert reference price). `category` backfilled. |
| `alerts` | Glitch events | `mrp` column (additive, backfilled); `reason` + `score`. |
| `darkstores` | Resolved stores per app | `lat/lon/label/eta_min`; `ON CONFLICT` upsert. |
| `watchlist` | Active + inactive SKU probe sets | `collections` (CSV labels: `home`, category names, `q:<term>`); `score DESC` orders; `first_seen_ts` backfilled; `is_digital_voucher` flag; `active` set by sweep (overflow = 0). Never deletes. |
| `stock_obs` | Per-cycle stock observations | `in_stock` (1/0/`NULL` — `NULL` = unknown/parse failure); `price`/`mrp`/`eta_min`; `source` (`home`/`collection`/`search`); `catalog_version` migration. |
| `oos_events` | Debounced out-of-stock events | `kind` (`oos` / `vanished`); `started_at` (first zero read); `ended_at`; `snapshots` (consecutive zero reads when open/closed); `restock_trigger`. Restart-proof rebuilt from `trailing_oos_streak`. |
| `searches` | Search query archive | `query`, `source` (`telegram`/`dashboard`/`cli`), `elapsed`, `n_results`, `top_*`. |
| `search_results` | Per-search result rows | `rank`, `platform`, `name`, `category`, `match`, `mrp`, `listed`, `discount`, `code`, `net`, `delivery`, `effective`, `url`, `note`. |
| `keyword_watches` | Telegram watch phrases | `chat_id`, `keyword` (lowercase); `created_ts`; `active`; `last_alerted_ts` (6h cooldown + rate cap). Deactivation only; rows kept. |
| `catalog_snapshots` | Full-catalog sweep snapshots | `(ts, app, store_id, sku_key)`; `name`, `price`, `in_stock`, `collections`. Idempotent per `(ts, sku)`. |
| `catalog_events` | Snapshot diff results | `kind` (`new`/`delisted`); pair reconciliation (`name,price`) excludes re-keys; mass-absence (<70% pair count) skips diff. |
| `embeddings` | Semantic vector cache | Keyed `(model, dims, name)`; `vector` (BLOB/text). Additive — provider switch re-keys cleanly. |
| `opportunities` | M7 persisted opportunity snapshots | Scored opportunity records (`gap_strength`, `DPI`, `coverage`, `churn`, `provenance`). |

### `inventory/inventory_<app>.db` (per-app; lazy-created by `Store`)

- `inventory_catalog` table: `id`, `ts`, `app`, `store_id`, `sku_key`, `name`, `price`, `mrp`, `in_stock`, `url`, `collections`, `category`, `raw_json`, `raw_json_truncated` (0/1), `raw_json_bytes`. `raw_json` capped at 16 KB (`_RAW_JSON_CAP`); truncated flag distinguishes capped from genuinely complete. `derive_product_url()` builds Blinkit `/prn/<slug>/prid/<id>` from `raw` payload when `url` absent.
- `catalog_snapshots` also written to `deals.db` (operational); inventory DB is the complete per-product record.

---

## 11. File map (current)

Evidence from `ls` and `AGENTS.md` repo map:

```
qcom-scraping/
  ARCHITECTURE.md          this file (updated 2025-09-08)
  README.md                 run guide + glossary (linked before contributing)
  GUIDE.md                  first-run + interpretation guide
  AGENTS.md                 multi-agent rules; invariant list; adapter state table
  DEMAND_RADAR.md            phase design + live status + proxy plan
  IMPLEMENTATION_LOG.md     single milestone log (M1-M7); never per-phase files
  PRODUCT_SPACE_PLAN.md     M1-M7 design + accepted/rejected decisions
  config.yaml               all tunables; miniyaml-compatible; secrets in .env only
  codes.yaml                user-editable delivery fees + offer codes
  run.py                    entrypoint; ALL CLI flags; feature catalog with editable args spec
  .env / .env.example       secrets (NVIDIA_Build_API_KEY, AI_API_KEY, etc.)
  deals.db                  main sqlite (WAL); all persistent data
  deals.demo.db             scratch DB for --demo (NEVER touches real DB)
  exports/                  locality JSON, embedding maps, DPI CSV, AI explain reports
  inventory/                per-app DBs + inventory_catalog snapshots
  inventory_milestones.md   inventory pipeline milestones
  docs/                    APK reverse findings (zepto, swiggy, blinkit); quick-commerce API vetting
  apks/                    APK files (gitignored; too large)
  .tools/ollama/            workspace-local Ollama binary; .ollama/ models; .ollama_home/ server
  .venv/                    Python environment (numpy / sklearn / plotly / plotly for embed_3d_map)
  src/                     core modules (see below)
  src/adapters/             7 adapters + base + demo
  scripts/                  self-managing scripts: live_sweep, test_embeddinggemma, compare_embeddings, embed_3d_map, bench_ollama_batch, embed_gemini_resume, embed_3d_map
  tests/                    test files
  backups/                  DB backups (deals.db.bak-*)
```

### Key `src/` modules

| File | Role | Key functions / notes |
|------|------|-----------------------|
| `run.py` | Entrypoint; CLI parsing (`_flag_*` helpers); `run()` dispatches all paths. | `--check` (config + adapter sanity); `--demo` (scratch DB); `--search`, `--bot`, `--ui` (with `--bot`/`--monitor`), `--once`; `--map-locality`, `--build-watchlist`, `--demand`, `--store-inventory`, `--catalog` (catalog inventory mode); `--embed-catalog`, `--similar`; `--product-fields`, `--product-space`, `--product-vectors`, `--detect-assortment-gaps`, `--product-density`, `--detect-gaps`, `--score-opportunities`, `--opportunity-report`, `--temporal-analysis`, `--validate-opportunities`, `--opportunity-pipeline`; `--demand-report`, `--catalog-report`; `--qc-status`; `--purge-vouchers`; feature catalog with `args` whitelist. |
| `src/store.py` | SQLite persistence (main class `Store`). | Schema (`_SCHEMA`), migrations (additive only), `record()`, `record_alert()`, `usual_price()` (excludes newest), `upsert_darkstore()`, `watchlist_for_store()`, `record_stock_obs()`, `open/close_oos_event()`, `trailing_oos_streak()`, `catalog_snapshot_list()`, `catalog_event_list()`, `record_catalog_snapshot()`, `catalog_prev_snapshot()` (pair reconciliation), `record_catalog_events()`, `deactivate_watchlist_except()` / `deactivate_watchlist_skus()`, `inventory_catalog` upsert (`_derive_product_url()`, `raw_json_truncated`), `purge_vouchers()`, `add/remove_watch()`, `today_cheapest_by_category()`. |
| `src/adapters/base.py` | Adapter interface + anti-block helpers. | `Adapter` class (`PROBE_URL`, `PROBE_TERMS`, `_rotate()`, `_browser_catalog_full()`); `load_proxies()`; `fresh_user_agent()`; `norm_price()`; `_stock_state()`; `_parse_catalog()`; process-group timeout + reap; stderr/stdout drain threads; `on_batch` incremental persistence. |
| `src/adapters/blinkit.py` | Blinkit adapter. | Overrides `APP_URL` / `HEALTH_URL`; `search()` merges `--pre` shelf results (`Cigarettes` shelf: 15119 → l1_cat 1948; `Cigar` 62439 → 3466; `Lighters` 383144 → 7778; `Rolling Needs` 11644 → 1982; `Paan Masala` 127957 → 2517). Text search yields accessories only; shelf provides real tobacco. |
| `src/adapters/zepto.py` | Zepto adapter. | `HEALTH_URL` = search route; `search()` uses signed responses. |
| `src/adapters/instamart.py` | Instamart adapter. | `APP_URL` = `instamart.in`; inner-container pagination (`POST /api/instamart/category-listing/filter/v2`); `--im-term` locality binding; `--mirror-page-ms` / `--mirror-stride`. |
| `src/adapters/amazon.py` | Amazon adapter. | Search + price-track. |
| `src/adapters/flipkart.py` | Flipkart adapter. | Search + price-track. |
| `src/adapters/jiomart.py` | Jiomart adapter. | IP-locked store resolution; `search()` works; rate-limited. |
| `src/adapters/bigbasket.py` | Bigbasket adapter. | `enabled: false`; Akamai 403. |
| `src/adapters/demo.py` | Demo adapter. | Injected glitch; scratch DB only. |
| `src/adapters/trackers.py` | Amazon / Flipkart price trackers. | `Tracker` class; `crawl()` for price tracking. |
| `src/locality.py` | Demand phase 1. | `anchor` grid + `landmark` seeds; `discover_darkstores()`; `resolve_store()`; `cluster_darkstores()`; `build_corridor()`; `rotate_anchor_term()` (process-local rotation was broken 09-02; fixed by passing config landmark names directly — `im_term` for landmark anchors = config name; grid anchors rotate through config names). `src/locality.py` self-test (`python3 -m src.locality`). |
| `src/watchlist.py` | Demand phase 2. | `WatchlistBuilder`; `build_for_store()` (one session/store); `build_all_stores()`; `catalog_inventory_mode()`; `catalog_inventory_mode()` deep-cats (`--catalog`); `pair_reconcile()` for snapshot diff. Self-test (`python3 -m src.watchlist`). |
| `src/prober.py` | Demand phase 3. | `Prober` class; `run_round()`; `observe_store()`; `record_obs()`; `reconcile_stale_events()`; `update_canary_state()`; `freeze_suspect()`; `update_watchlist()`; `build_report()`. Self-test (`python3 -m src.prober`). |
| `src/demand.py` | Demand phase 4. | Pure sqlite functions: `dpi_table()`, `heatmap()`, `eta_curve()`, `demand_summary()`, `export_csv()`. |
| `src/search.py` | Search engine. | `SearchEngine` class; `search()`; `execute_parallel()`; `load_product_space()` call for entity resolution; `match_score()`; `blend_score()` (max of token + semantic). |
| `src/pricing.py` | Effective price calculation. | `PricingEngine`; `calculate()`; reads `codes.yaml`. |
| `src/categories.py` | Keyword classifier. | `categorize()`; ordered rules. |
| `src/dashboard.py` | Dashboard server. | Flask-like endpoints; `start_feature()` / `stop_feature()` / `get_feature_status()` / `get_feature_log()`; `FEATURE_CATALOG` with editable `args` whitelist. |
| `src/tgbot.py` | Telegram bot. | `Bot` class; `/watch`, `/unwatch`, `/digest`, `list_watches()`; `WatchPusher` (matches search results / crawl batches against `keyword_watches`; rate-capped + cooldown); `start()` / `poll()` loop. |
| `src/orchestrator.py` | Monitor loop scheduler. | `Orchestrator` class; `schedule` config (`stagger`/`jitter`/`waf_failover`); `run_cycle()`; `WafFailover` strike ledger (`N` consecutive empty/errored → gate for `gate_cooldown_cycles`; other app gated → `blinkit_priority_extra` extra passes); event emission (`/status` shows `gate`/`skip`/`boost`). `protected_apps` = `blinkit`. Self-test (`python3 -m src.orchestrator`). |
| `src/events.py` | Thread-safe event bus. | `EventBus`; `publish()` / `subscribe()`; feeds `/status`. |
| `src/alert.py` | Alert fan-out. | `AlertPusher`; desktop + telegram + log (`deals.log`); carries reference price (`mrp` / `usual_price`). |
| `src/detect.py` | Glitch detector. | `GlitchDetector`; `score()`; `z-score` against local per-store baseline. |
| `src/honey.py` | Honey-pot SKU basket. | `HoneyBasket`; `load()`; `check()`. |
| `src/miniyaml.py` | YAML subset parser. | Stdlib fallback when `PyYAML` absent; parses `config.yaml`. |
| `src/ai_assist.py` | AI assistant. | `AIExplain`; `explain_demand()`; `suggest_focus()`; `suggest_methodology()`; `INT_PARAMS` whitelist enforcement; output saved to `exports/ai_explain_<stamp>.md`. |
| `src/embed.py` | Embedding client. | `EmbedClient`; NVIDIA (`urllib`) + local Ollama (`requests`); batch 256 (Ollama) / 1000 (NVIDIA); `embeddings` table read/write; `search_vectors()`; `backfill_catalog()`. Self-test (`python3 -m src.embed` — fake transport). |
| `src/inventory.py` | Catalog inventory module. | `InventoryCapture`; `run_sweep()`; `build_catalog()`; `resolve_location()` (uses `lat`/`lon` override from `--lat`/`--lon` or `deals.db.darkstores`); `store_inventory()` writes both `inventory_catalog` (inventory DB) and `catalog_snapshots` (deals.db); captures `raw_json` with cap/truncated flag. |
| `src/product_fields.py` | Grocery product parser. | `parse_name()` — brand, pack_value/pack_unit, `is_multipack`, variant, `unit_base`, `unit_price`, `parse_status`. Self-test (`python3 -m src.product_fields`). |
| `src/geo.py` | Geo layer. | `GeoResolver`; `resolve_darkstore()`; `build_grid()`; `anchor` + `landmark` definitions. |
| `scripts/live_sweep.py` | One-shot real-glitch hunt. | Runs full crawl across stations; outputs to terminal + file. |
| `scripts/test_embeddinggemma.py` | Local Ollama demo. | Starts `ollama serve`, pulls `embeddinggemma`, runs demo queries. |
| `scripts/compare_embeddings.py` | Quality comparison. | Compares `embeddinggemma` vs NVIDIA neighbors (numpy); needs `.venv/bin/python`. |
| `scripts/embed_3d_map.py` | Inventory atlas. | `PCA-50` → `k-means` → `PCA-3` / `PCA-2` (`UMAP` optional); renders interactive `exports/embedding_map.html` / `embedding_map_gemma.html`. Self-test with `--sample N`. |
| `scripts/bench_ollama_batch.py` | Batch-size benchmark. | Confirms B=256 optimum; B≥512 kills runner (connection reset by peer). |
| `scripts/embed_gemini_resume.py` | Gemini backfill. | `gemini-embedding-001` @768; `embedding_send_extras: false`; batch 100 + 16s pause ≈ 94 RPM/key; 4 keys ≈ 4k names/day; resets midnight PT. |

---

## 12. Current status (evidence-based)

Evidence: `IMPLEMENTATION_LOG.md`, `AGENTS.md` phase table, `README.md`, `DEMAND_RADAR.md`, `run.py --check` output (verified clean), `git status` (clean at `main` `b7f3502`), `node --check tools/pw_catalog.js`, `python3 -m py_compile` on touched files.

- **P1 locality:** BUILT (`locality.py` + `geo.py` + `exports/locality_*.json`).
- **P2 watchlist:** BUILT (`watchlist.py`; `--catalog` deep-cats; pair reconciliation; voucher purge; `catalog_events` new/delisted; `inventory.py` full sweep).
- **P3 prober:** BUILT (`prober.py` + `store.py` `oos_events`; debounce; streak reconstruction; vanished guard; suspect-cycle freeze; stale event reconciliation; `catalog_inventory` snapshot + event pipeline).
- **P4 analysis:** BUILT 08-22 (`demand.py`; DPI rollups; heatmap; ETA curve; `demand_summary`; CSV export; `/demand`, `/heatmap`, `/eta` endpoints; `--demand-report` / `--catalog-report`).
- **P5 hardening:** WIRED (`proxy` plumbing: config + env + adapter `pick_proxy()` + `pw_catalog.js` `--proxy` + process-group isolation). **PENDING:** procure Mumbai-exit residential proxy pool; validate IP↔anchor coherence live (jiomart store change per IP); stagger/jitter + global req/hr cap + backoff per `DEMAND_RADAR.md` Phase 5.
- **Product Space M1–M7:** M1 (union layer) BUILT; M2 (vectors) BUILT; M3 (assortment gaps) BUILT; M4 (density) BUILT; M5 (detect-gaps) BUILT; M6 (`score-opportunities` + `--persist`) BUILT; M7 (`opportunity-report` + temporal analysis + validation) BUILT.
- **Embeddings:** NVIDIA provider verified (batch 1000, 39 requests / 14.5 min for 44,487 names). Ollama (`embeddinggemma`) verified locally (`scripts/test_embeddinggemma.py`; `scripts/compare_embeddings.py`). `embeddings` table additive.
- **Dashboard:** All listed endpoints verified; feature panel editable args spec enforced; location editor validates both YAML loaders + backups to `/tmp`; AI panel whitelist enforced (`INT_PARAMS`); `/shutdown` reaps children.
- **Adapters:** All 7 present (`amazon`, `flipkart`, `blinkit`, `zepto`, `instamart`, `jiomart`, `bigbasket`). `bigbasket` `enabled: false`. `jiomart` enabled but with documented block. `amazon`/`flipkart` search + price-track working.
- **Schema:** Additive migrations verified (`category` column on `price_obs`; `mrp` on `alerts`; `catalog_version` columns; `restock_trigger` / `voucher_type` / `promotional_context` on `stock_obs`; `is_digital_voucher` / `first_seen_ts` on `watchlist`; `first_seen_ts` backfill; `first_seen_ts` / `catalog_version` / category backfills present).

---

## 13. How to read the updated architecture

This file is the evidence-based source of truth. The `ARCHITECTURE.md` update (2025-09-08) was produced by inspecting:

- `AGENTS.md` (repo identity, adapter table, invariant rules, command list, environment quirks).
- `DEMAND_RADAR.md` (phase design, P5 proxy plan, voucher exclusion, snapshot-driven delisting, suspect-cycle freeze, restart-proof streaks, silent-hour coverage).
- `README.md` (install, commands, feature descriptions, adapter expansion notes).
- `IMPLEMENTATION_LOG.md` (M1–M7 milestones; deviation comments).
- `PRODUCT_SPACE_PLAN.md` (pipeline design; accepted/rejected decisions).
- `run.py` (CLI flags; feature catalog; entry dispatch).
- `config.yaml` (tunables; `schedule` / `anti_block` / `demand` / `ai` / `geo` sections).
- `src/store.py` (full sqlite schema + migrations + persistence methods).
- `src/adapters/*.py` (adapter behavior + state).
- `src/locality.py` / `watchlist.py` / `prober.py` / `demand.py` (phase implementations).
- `src/search.py` / `pricing.py` / `categories.py` / `tgbot.py` (search + bot + pricing).
- `src/dashboard.py` (dashboard endpoints + feature management + AI assistant).
- `src/embed.py` / `.tools/ollama/` / `scripts/` (embedding layer + local server).
- `tools/pw_catalog.js` (crawler internals; geo seed; deep-cats; mirror pagination; multi-tab; timeout/reap; proxy pass-through).

When another agent edits the repo, verify against this file before proceeding. Undocumented behavior will break by the next edit.
