# M1 Implementation Log — Product-Space Intelligence (capture + normalization)

Tracking the build-out of **M1** from `PRODUCT_SPACE_PLAN.md` (the tailored
"Product-Space Intelligence Engine" plan), restricted to **Phases 1–2**
(grocery product-fields parser + cross-DB union layer) plus the store-targeted
full-category inventory capture the user asked for.

The plan was reviewed and adjusted before coding; the approved spec is the
"revised per review" plan. Key review-driven decisions baked into this build:

1. **Cross-app identity = entity resolution, not a bare `match_score >= 0.5`.**
   Hierarchy: exact canonical key → matcher as *candidate generator* →
   **pack-size / variant vetoes** → semantic confirmation only.
2. **`collections` (app taxonomy) vs `category` (internal taxonomy)** kept
   explicitly distinct.
3. **`raw_json` truncation fields**: `raw_json_truncated` + `raw_json_bytes`.
4. **Provenance round-trip** must hold: every normalized record traces back to
   its `inventory_catalog` row → `raw_json`.

---

## Status table

| # | Phase / component | Step | Status | Verification |
|---|---|---|---|---|
| 1 | Phase 1 — `src/product_fields.py` | Name parser (brand/pack/unit/variant/unit_price) + self-test + `--report` | ✅ Done | `python3 -m src.product_fields` → 20/20 |
| 2 | A.3 — `src/store.py` | `upsert_inventory_catalog` (lazy table, raw_json + truncation fields) | ✅ Done | offline round-trip + truncation test |
| 3 | A.1 — `tools/pw_catalog.js` | Emit `raw` node per product + carry on re-sight | ✅ Done | `node --check` OK |
| 4 | A.2 — `src/watchlist.py` | Carry url/mrp/raw in `_build_store`; `build_one_store` + `inventory_db` thread-through | ✅ Done | compiles; `build_one_store` present |
| 5 | A — `src/inventory.py` | `run_inventory` → single-store full-category; drop `approx_location` | ✅ Done | `python3 -m src.inventory` self-test OK |
| 6 | Phase 2 — `src/product_space.py` | Union layer + entity-resolution identity + provenance | ✅ Done | `python3 -m src.product_space` + deals.db smoke |
| 7 | `run.py` | `--store-inventory` (--app/--store req), add `--product-fields`/`--product-space` | ✅ Done | `--check` + `--product-fields` OK |
| 8 | `src/dashboard.py` | Update `store_inventory` FEATURE_CATALOG args | ✅ Done | (manual) |
| 9 | Docs | README + AGENTS: Commands, Repo map, collections/category distinction | ✅ Done | README prose+commands; AGENTS repo map + commands |
| 10 | Tests | `tests/` (stdlib unittest) incl. provenance round-trip | ✅ Done | `python3 -m unittest` → 12/12 OK |
| 11 | A.4 — incremental capture | `tools/pw_catalog.js` `emitVisitBatch` per-visit JSON + `base.py` main-thread streaming + `watchlist.py` flush-every-2-visits buffer | ✅ Done | `node --check` OK; `tests/test_inventory_capture.py` → 2/2; full suite 14/14 OK |
| 12 | A.5 — product URL | `Store._derive_product_url` (Blinkit `/prn/<slug>/prid/<id>` from captured `product_id`; other apps untouched) + backfill of 795 existing rows | ✅ Done | derivation unit-checked; all 795 `inventory_blinkit.db` rows now have canonical url |

---

## Detailed steps

### 1. `src/product_fields.py` (Phase 1 parser)
- `parse_name(name, price=None, category=None)` → dict with `brand, pack_value,
  pack_unit, is_multipack, variant, unit_base, unit_price, parse_status`.
- Pack parsing: volume (`ml`/`l`→`ml`), weight (`g`/`kg`→`g`), count (`pack of
  N`, `N pcs`, `Pages`, `Pulls`, …), multipack (`N x <size>`; flavour separators
  like `Mango x Chilli` are NOT treated as multipacks). `_` allowed as a
  number–unit separator (`750_Ml`).
- `unit_price` = price per canonical base (₹/L for ml, ₹/kg for g, ₹/each for
  count). **`no_pack` → `unit_price=None`, never 0.**
- Brand: curated multi-word list (`Metro Living`, `Mother Dairy`, `India Gate`,
  `Coca Cola`, …) + first-capitalized-token heuristic; apostrophes normalized
  (`Lay's`→`Lays`, `Baker's Loaf`→`Bakers Loaf`) so grouping keys are stable.
- CLI: `--product-fields` (self-test) / `--product-fields --report --sample N`
  writes `exports/product_fields_sample.csv` with status tallies (the ≥80%
  accuracy gate is a human review of that CSV).
- **Verification:** `python3 -m src.product_fields` → `self-test OK: 20/20`.

### 2. `src/store.py` — `upsert_inventory_catalog`
- Added (no deals.db schema change): lazily `CREATE TABLE IF NOT EXISTS
  inventory_catalog` — created only on inventory DBs. Columns: `app, store_id,
  sku_key, name, price, mrp, in_stock, url, collections (SOURCE/APP taxonomy),
  category (INTERNAL taxonomy), raw_json, raw_json_truncated, raw_json_bytes`.
- `raw_json` = `json.dumps(rec.raw)`, capped at 16 KB; `raw_json_truncated=1`
  and true byte length in `raw_json_bytes` when capped. `REPLACE` on
  `(app, store_id, sku_key)` → latest snapshot per run.
- **Verification:** offline round-trip (`k1`/`k2` upserts) + a >16 KB raw
  confirmed `raw_json_truncated=1, length=16384, bytes=20009`.

### 3. `tools/pw_catalog.js` — emit `raw`
- `collect()` and `collectLdJson()` now attach `raw: node`/`raw: src` to each
  product record; re-sight refresh keeps `prev.raw = rec.raw` (latest wins).
- **Verification:** `node --check tools/pw_catalog.js` → OK.

### 4. `src/watchlist.py` — carry-through + single-store entrypoint
- `_build_store` aggregation now keeps `url, mrp, raw` (previously dropped).
  **Before** the voucher-exclusion block, every product is written to
  `self.inventory_db.upsert_inventory_catalog(...)` — i.e. the COMPLETE
  inventory (including vouchers) is captured; filtering stays downstream.
- `WatchlistBuilder.__init__(cfg, db, inventory_db=None)`; new public
  `build_one_store(app, store_id, lat, lon, catalog=True, …)` calls `_build_store`
  with no search terms → every-category sweep. Does NOT require the store in
  `darkstores` (caller supplies lat/lon).
- **Verification:** compiles; `build_one_store` present with expected signature.

### 5. `src/inventory.py` — `run_inventory` rewrite
- Now **single-store, full-category**: requires `--app` + `--store`. Location:
  `--lat/--lon` override, else looked up from `deals.db.darkstores`, else
  `SystemExit`. Opens `inventory_<app>.db`, drives `WatchlistBuilder(...,
  inventory_db=inv_db).build_one_store(...)`, reports SKU/url/raw counts.
- Removed dead `approx_location`; kept `build_locality_cfg` (used by self-test).
- **Verification:** `python3 -m src.inventory` → anchor-count + rich-capture
  round-trip OK.

### 6. `src/product_space.py` — union + identity + provenance
- `load_product_space(apps=None, since=None, attach_embeddings=False, root=None)`
  reads (read-only, `mode=ro`) `inventory_<app>.db.inventory_catalog` as primary,
  and falls back/union with `deals.db.catalog_snapshots` (latest per
  sku) for stores not yet rich-captured. (M1 does NOT join `watchlist.active` —
  an optional active-filter is left for M2 when the product space feeds Demand
  Radar; the union is deliberately the full catalog surface.)
- Vouchers excluded via `is_voucher_name`. Each row carries `app, store_id,
  sku_key, inventory_db` → provenance.
- `assign_product_groups(rows)` implements the entity-resolution hierarchy:
  exact canonical key → `match_score≥0.5` candidate generation (within brand)
  → **pack/variant veto** → optional semantic cosine confirmation. Sets
  `product_group_id`, `group_method` (`exact|fuzzy|semantic|new`),
  `group_confidence`.
- `attach_embeddings` (default off) lazily joins `deals.db.embeddings` for the
  current name set (capped at 4000 distinct names) — an M2 hook.
- **Verification:** `python3 -m src.product_space` → 500ml merged across apps,
  1L separate (pack veto); deals.db-only smoke → 4 rows / 2 groups, 500ml shared.

### 7. `run.py` — CLI
- `--store-inventory` now requires `--app`+`--store`, passes `--lat/--lon/
  --mirror-page-ms/--tabs`. Added `--product-fields` and `--product-space`
  (`--csv` export to `exports/product_space.csv`).
- **Verification:** `python3 run.py --check` OK; `python3 run.py --product-fields`
  → 20/20.

### 8. `src/dashboard.py` — FEATURE_CATALOG
- `store_inventory` entry `args` updated to `--app`+`--store` (required) +
  `--lat/--lon/--mirror-page-ms/--tabs`; description/meta reflect single-store
  full-category capture.

### 9. Docs — README + AGENTS
- **README.md:** the Commands examples for `--store-inventory` now show the
  required `--app <app> --store <store_id>` form + `--lat/--lon/--mirror-page-ms/
  --tabs`, and two new examples (`--product-fields`, `--product-space [--csv]`)
  were added. The "Store inventory near you" prose block was rewritten to
  describe single-store full-category rich capture into `inventory_catalog`
  (`collections` = app taxonomy, `category` = internal taxonomy, `raw_json`
  opaque), with the `deals.db` operational snapshot kept, and the "Run it alone"
  warning retained.
- **AGENTS.md:** Repo map gained `inventory.py` (single-store capture),
  `product_fields.py`, `product_space.py`; the Commands block for
  `--store-inventory` was rewritten to the new form and `--product-fields` /
  `--product-space` examples added.

### 10. Tests — `tests/` (stdlib `unittest`)
- `tests/__init__.py`, `tests/test_product_fields.py`, `tests/test_product_space.py`.
- `test_product_fields`: mirrors the 20 curated `SELFTEST_CASES` (brand/value/
  unit/multipack) plus explicit invariants — `no_pack` ⇒ `unit_price=None`
  (never 0), missing price keeps `unit_price=None`, apostrophe normalization
  (`Lay's`→`Lays`), underscore unit separator (`750_Ml`), multipack count,
  unparseable input. **All pass.**
- `test_product_space`: builds throwaway sqlite files under a temp root and
  asserts (a) **provenance round-trip** — every normalized record traces back to
  its `inventory_catalog` row → `raw_json`; (b) entity resolution — cross-app
  500ml merge to ONE group, 1L is its own group (pack veto), vouchers excluded;
  (c) `deals.db.catalog_snapshots` fallback union; (d) no double-counting when
  the same sku exists in both sources (rich path wins). **All pass.**
- **Verification:** `python3 -m unittest tests.test_product_fields
  tests.test_product_space` → `Ran 12 tests … OK`. Plus `python3 -m
  src.product_fields` (20/20) and `python3 -m src.product_space` self-tests,
  `node --check`, `py_compile` of all touched files, and `run.py --check`.

---

### 11. Incremental capture (fix: "stop midway loses everything")
- **Root cause (diagnosed on a live run Ctrl-C'd at ~2981 SKUs):** `_browser_catalog_full`
  returned **all** products only AFTER the browser sweep fully completed, and
  `_build_store` called `upsert_inventory_catalog` only **after** that return. A
  mid-run stop persisted nothing — the `~2981 SKUs` seen were the crawler's live
  stderr `[sweep]` counter (the deduped `products` Map size), **not** persisted
  rows. `inventory_blinkit.db.inventory_catalog` confirmed 0 rows after the stop.
- **Fix — stream per-visit batches:**
  - `tools/pw_catalog.js`: new `emitVisitBatch(v, before)` computes the delta of
    `products` keys not in a `before` Set and `console.log`s
    `{"type":"batch","label","count","products":[...]}` once per `sweepVisit`
    (both the multi-tab and single-tab sweep loops now wrap the call with a
    `before` Set). The terminal summary JSON (emitted last) carries **no** `type`
    key, so the Python side can tell batches apart from the summary.
  - `src/adapters/base.py`: `_browser_catalog_full` + `deep_sweep` gain an
    `on_batch=None` param. stdout is now parsed **inline on the main thread** (a
    stderr drain thread is kept for progress lines). Each `type:'batch'` line is
    dispatched to `on_batch` immediately; the terminal summary (last non-batch
    JSON) becomes the returned `prods`/`meta`. A reaper watchdog thread SIGKILLs
    the browser process group once the helper itself has exited, preventing a
    linger-hang if grandchildren hold the stdout pipe write-ends.
  - `src/watchlist.py`: `_build_store` adds a buffered `_persist_batch` closure
    (appends each batch's records to `_inv_buf`, increments a visit counter, and
    flushes via `_flush_inv_buf` when `visits % 2 == 0`) passed as `on_batch` to
    `deep_sweep`. Inventory is written **every 2 visits**, on the main thread
    (sqlite-safe). The existing post-sweep `agg` upsert loop is preserved as an
    idempotent completeness pass on normal completion (it also covers the odd
    visit whose batch is still buffered). A hard kill loses at most ~1 visit.
- **Verification:** `node --check tools/pw_catalog.js` OK; `py_compile` of
  `base.py`/`watchlist.py`/`store.py` OK; `run.py --check` OK.
  `tests/test_inventory_capture.py` (new) simulates a crawl that streams 5
  batches then returns `[]` (interrupted, no terminal summary): it asserts 12
  rows persisted (batches 0–3, flushed at visits 2 and 4) with batch 4 buffered
  and lost — the accepted "~1 visit" loss — plus a full-run variant asserting all
  15 rows via the terminal summary. Full suite: `python3 -m unittest` → 14/14 OK.

### 12. Product URL for Blinkit (requirement: "including product's url")
- **Investigation (user chose "find real URL in payload"):** ran a short Blinkit
  crawl with `DSH_BODY_DIR` dumping every intercepted JSON response and grepped
  all bodies. **Blinkit's catalog API does NOT return a per-product URL in any
  intercepted endpoint** — every `url` key is a CDN image URL (`cdn.grofers.com`)
  or an ad-tracker beacon; every `link` key is footer nav (`/terms`, `/faq`…);
  product nodes carry `product_id` (or `id`) but no `url`/`web_url`/`seo_url`/
  `slug`. The already-captured 419 `inventory_catalog` rows confirmed the same
  (raw has `product_id`, no url field). Zepto/Instamart payloads do carry a URL
  and are left untouched (the crawler already lifts `node.url||node.link||
  node.seo_url`).
- **Resolution:** derive the canonical Blinkit URL — its real, working shape is
  `/prn/<slug>/prid/<product_id>` (per AGENTS.md), and `product_id` is already
  captured in `raw`. Added `Store._derive_product_url(app, rec)` called from
  `upsert_inventory_catalog` (the single write chokepoint, mirroring how
  `category` is derived from `name` there). It preserves an explicit `url` if
  present, derives for Blinkit from `raw.product_id` (fallback `sku_key`), and
  returns `''` for other apps (never invents a URL). Verified: Blinkit
  no-url→`https://blinkit.com/prn/mysore-sandal-soap-75-g/prid/19934`; explicit
  url preserved; Zepto no-url→''; Zepto explicit url preserved.
- **Backfill:** the 795 existing `inventory_blinkit.db` rows (a live partial
  run had grown from 419→795) had empty `url`; backfilled all 795 with the
  derived canonical URL (idempotent, only filled empty values).

---

## Comments & deviations

_(updated as work completes)_

- **Deviations from the written plan:**
  - *`attach_embeddings` default OFF.* The plan said "joined with embeddings
    when present." Loading 44k+ 2048-d vectors (~350 MB) into the union on every
    run is wasteful for M1; the semantic-confirmation branch degrades gracefully
    without them. The join is implemented and behind `--embeddings` / a flag for
    M2. **Rationale:** ponytail — don't pay a 350 MB cost until something needs it.
  - *Union reads `deals.db.catalog_snapshots` as a fallback,* not only
    `inventory_catalog`. The approved plan said read BOTH, but the rich
    `inventory_catalog` table is empty until a real `--store-inventory` run
    happens. Falling back to `catalog_snapshots` keeps the union useful
    immediately and honours "the union reads both repos."
  - *Brand tokens are apostrophe-stripped* (`Lay's`→`Lays`) for stable grouping
    keys, rather than preserving the source apostrophe. The self-test was aligned
    to this.
  - *Grouping algorithm optimized for scale (discovered during verification).*
    The first cut of `assign_product_groups` scanned **all** groups for every
    row to find an exact canonical-key match — O(n·groups), which never finished
    on the real `deals.db` (1.36 GB, 167k catalog_snapshots rows). Changed to an
    O(1) `by_exact` hashtable for exact matches and precomputed token frozensets
    (`_sim` over cached sets instead of re-tokenizing per candidate) for the
    fuzzy step. End-to-end `run.py --product-space` over the full real dataset
    dropped from "hangs >60s" to ~33s (22.6s parse + 10.3s grouping, 13,686
    groups). **This is a deviation from the literal plan** (which only specified
    the resolution *hierarchy*, not the implementation's complexity bound) but is
    required for the feature to be usable on real data — ponytail: "pick the
    edge-case-correct option when two stdlib approaches are the same size."
- **Implementation notes:**
  - `_brand_span` maps an apostrophe-stripped needle back to the original-case
    span in `name` so multi-word brands like `Metro Living` are extracted
    correctly.
  - `raw_json` cap of 16 KB is a deliberate safety valve; the `raw_json_bytes`
    + `raw_json_truncated` pair makes a capped payload distinguishable from a
    genuinely small one (the review's explicit request).
  - `inventory_catalog` is created lazily only where written, so `deals.db`
    schema is untouched (additive-only discipline preserved).
  - The `deals.db` fallback query was kept as the row-value `IN (GROUP BY)`
    form (fast: ~2.5s on 167k rows) — the earlier hang was purely the
    O(n·groups) grouping, not SQL.
- **Open items / follow-ups (not M1):**
  - A real `--store-inventory` crawl has not been executed in this session
    (it requires a live browser crawl + ~20–60 min/store and must run alone).
    The capture path is verified offline (incl. the interruption simulation in
    `tests/test_inventory_capture.py`). **With incremental capture, a live run
    can now be stopped early and the partial `inventory_catalog` it wrote every
    2 visits is retained** — so the end-to-end acceptance gate no longer requires
    an uninterrupted full sweep; the user can validate the capture path on a short
    partial run and inspect `inventory_<app>.db.inventory_catalog`. The live `inventory_blinkit.db`
     already shows this working (grew 419→795 rows from a stopped partial run, all
     with derived product URLs).
  - M2 (vectors + Atlas) is where `attach_embeddings` and the semantic
    confirmation become first-class.
