# Implementation Log — Product-Space Intelligence Engine

Consolidated milestone log for `PRODUCT_SPACE_PLAN.md` (the tailored
"Product-Space Intelligence Engine" plan). **This is the ONLY log file** — per
AGENTS.md, no per-phase files (`M1_*.md`, `M2_*.md`, …) are used. Update it
proactively as each step completes; keep deviation comments as one-liners that
state the reason.

Plan phases: M1 = Phase 1+2 (parser + union) + store-capture rewrite. M2 =
Phase 3 (vectors) + Phase 4 (atlas) + the parser-alignment prep deferred from
M1. Rollout beyond: M3 assortment gaps, M4 density, M5 scoring, M6 LLM briefs
(see plan's final rollout table).

---

## M1 — Product-Space capture + normalization (Phases 1–2 + store capture)

### Status table
| # | Component | Step | Status | Verification |
|---|---|---|---|---|
| 1 | `src/product_fields.py` | `parse_name` (brand/pack/unit/variant/unit_price) + self-test + `--report` | ✅ | `python3 -m src.product_fields` → 20/20 |
| 2 | `src/store.py` | `upsert_inventory_catalog` (lazy, raw_json + truncation fields) | ✅ | round-trip + truncation test |
| 3 | `tools/pw_catalog.js` | emit `raw` per product | ✅ | `node --check` |
| 4 | `src/watchlist.py` | carry url/mrp/raw; `build_one_store` + `inventory_db` thread-through | ✅ | compiles; `build_one_store` present |
| 5 | `src/inventory.py` | `run_inventory` single-store full-category rewrite | ✅ | `python3 -m src.inventory` OK |
| 6 | `src/product_space.py` | union + entity-resolution identity + provenance | ✅ | `python3 -m src.product_space` + deals.db smoke |
| 7 | `run.py` | `--store-inventory` (`--app/--store`), `--product-fields`, `--product-space` | ✅ | `--check` + `--product-fields` OK |
| 8 | `src/dashboard.py` | `store_inventory` FEATURE_CATALOG args | ✅ | manual |
| 9 | Docs | README + AGENTS: Commands, Repo map, collections/category split | ✅ | README + AGENTS updated |
| 10 | Tests | `tests/` (stdlib unittest) incl. provenance round-trip | ✅ | `python3 -m unittest` → 12/12 |

### Comments & deviations (one-liner, with reason)
- `attach_embeddings` default OFF — ponytail: skip the ~350MB join until a consumer needs it (plan said "join when present").
- union reads `deals.db.catalog_snapshots` as fallback — `inventory_catalog` is empty until a real `--store-inventory` run lands, so the union stays useful immediately.
- brand tokens apostrophe-stripped (`Lay's`→`Lays`) — stable grouping keys; avoids false brand mismatch across apps.
- `assign_product_groups` uses O(1) exact hashtable + precomputed token sets — original O(n·groups) scan never finished on the 167k-row deals.db (1.36GB); now ~33s for 13,686 groups.
- `inventory_catalog` created lazily only on inventory DBs — additive-only discipline; `deals.db` schema untouched.

---

## M2 — Vectors + atlas + parser alignment (Phase 3 + 4 + deferred parser prep)

### Status table
| # | Component | Step | Status | Verification |
|---|---|---|---|---|
| 1 | Prep (deferred from M1) | Reference-DB alignment: validate + improve `parse_name` via `reference/`; build `match_reference(name)` | ⏸ Pending — do tomorrow | accuracy gate ≥80% on labeled sample |
| 2 | Phase 3A | Semantic vector reuse + feed parsed fields as context (`brand\|category\|variant\|pack_size`) | ☐ | `attach_embeddings` path in `product_space.py` |
| 3 | Phase 3B | Attribute vector (`unit_price`, `pack_size_value` norm, `is_multipack`, `category_depth`), z-scored per category | ✅ | `src/product_vectors.py`; `python3 -m src.product_vectors` OK; `--product-vectors` CLI |
| 4 | Phase 3C | Commercial vector (`dpi`, `store_count`, `app_count`, `days_since_first_seen`, `is_active`, `churn_flag`) | ✅ | from Demand Radar rollup + catalog_events; degrades offline (no `db`) |
| M3 | Phase 6 | Assortment gaps (cross-app coverage signal; confidence = establishment only) | ✅ | `src/assortment_gaps.py` + tests OK; `--detect-assortment-gaps` CLI (12,747 on live union) |
| 5 | Phase 4 | Atlas: Gap mode + Unit Price mode + cross-app overlay toggle | ☐ | standalone HTML; load time not regressed (next) |
| M4 | Phase 5 | Per-category density + kNN distance + `insufficient_coverage`/`stale_coverage` guards | ✅ | `src/density.py` + tests OK; `--product-density` CLI (2,606 trustworthy sparse on live union) |

### Reference data already in `reference/` (fetched 2026-09-07)
| File | Rows | Pack ground truth? | Role |
|---|---|---|---|
| `BigBasket.csv` (Kaggle `chinmayshanbhag/big-basket-products`) | 8,208 | ✅ `Quantity` **100%** | primary pack GT — India grocery |
| `amazon_india_products.csv` (HF `pgurazada1/amazon_india_products`) | 30,000 | ✅ `Pack Size Or Quantity` **99%** | secondary pack GT — broader mix |
| `BigBasket Products.csv` (Kaggle `surajjha101/...`) | 38,341 | ❌ | brand + sub_category vocab (no pack col) |
| `Indian Packaged Foods Nutritional Composition Data/packaged_foods_india.csv` (Mendeley) | 852 | ⚠️ `Serving_Size_g` | held-out eval / nutrition join |
| `openfoodfacts_india.csv` (OFF API) | 999 (partial) | ✅ `quantity` | ODbL, resumable via `fetch_off_india.py` |

### Step 1 plan (tomorrow)
1. Build labeled eval: `BigBasket.csv.Quantity` + `amazon_india_products.csv."Pack Size Or Quantity"` as ground truth, fuzzy-match to our `inventory_catalog`/`catalog_snapshots` names, score `parse_name` → real ≥80% gate.
2. Add `match_reference(name)` in `src/product_fields.py`: regex first → exact/fuzzy match vs reference `Quantity`/`Pack Size` → record `parse_source` (provenance).
3. Mine labeled pairs to extend unit dictionary + multi-word brand list (cheapest parser improvement).
4. Reserve Mendeley 852 as never-trained held-out eval.

### Comments & deviations (one-liner, with reason)
- reference-DB alignment slotted into M2 not M1 — user: parser stays M1 regex default; richer extraction deferred to M2.
- alignment uses stdlib `csv` only, no pandas — AGENTS.md no-pandas rule; OFF full `food.parquet` (7.8GB) deliberately NOT downloaded.
- OFF India pulled via API-filter (India only) + resumable fetcher — OFF edge-blocks this IP (503/401), only 999 rows landed; rest fetched later.
- Mendeley-publishing of our own catalog noted as future idea — product-level data only (no PII), low redistribution risk; choose CC0/CC-BY at upload.
- M2 (3B/3C) + M3 + M4 implemented directly in-thread, not via subagents — background subagents were terminated by the environment mid-run and the requested `qwen-gate` provider is disallowed in this session, so the work was done inline to keep the milestone moving.
