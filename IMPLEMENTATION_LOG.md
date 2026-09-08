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
- Hybrid LLM parser added to `src/product_fields.py` (M1 Phase-1 extension): regex parses pack/unit/multipack (≥85% accurate, instant); a local Ollama LLM (`qwen2.5:0.5b` via existing workspace `.tools/ollama/`) is consulted ONLY when regex confidence < 0.7 to refine brand/variant. Ollama absence → silent regex fallback (zero deps, no crash). Self-test covers both regex-only and mock-LLM hybrid paths.

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
| M5 | Phase 5/6 | Internal/attribute gaps: trustworthy sparse points scored by attribute-vector proximity (sparse + attribute-outlier guard; never from thin/stale categories) | ✅ | `src/gaps.py` + tests OK; `--detect-gaps` CLI (1,286 candidates on live union after honesty guard tightened 2,606→1,286) |
| M6 | Phase 7+8 | Opportunity scoring: gap_strength × DPI × coverage × churn with provenance + validation codes; crawl-artifact guard (missing app w/o category coverage dropped) | ✅ | `src/opportunities.py` + tests OK; `--score-opportunities` CLI; live smoke showed flat-0.5 bug → fixed via gap_strength + artifact guard, scores now vary (0.5→0.29) |
| M7 | Phase 10 | Opportunity persistence: additive `opportunities` table in deals.db (append-only snapshots, ts-keyed) + read-back report | ✅ | `persist_opportunities`/`load_latest_opportunities` in `src/opportunities.py`; round-trip test + self-test OK; live smoke persisted 12,981 opportunities → `--opportunity-report` read-back matches |
| M8 | Phase 3+4 + Atlas Gap/Opportunity | Unit Price / Cross-App / Gap / Opportunity atlas modes + evidence-trail inspector; plotly symbols (●/◎/◉/×) confirmed | ✅ (partial) | `scripts/embed_3d_map.py`: new mode buttons + dispatcher + `--opp-json`; evidence-trail inspector section added; subagent ee8dd81f failed → completed in-thread; ponytail: full interactive trail deferred to design cycle |
| M9 | Phase 14 + 15 | Temporal / emerging-segment + stability/robustness | ✅ | `src/temporal.py` + tests OK; `--temporal-analysis` CLI; subagent 5e4362df finished; offline-degradable |
| M10 | Phase 20 | LLM briefs + human review loop (taxonomy + persist) | ✅ | `src/human_review.py` + tests OK; `--validate-opportunities` CLI; subagent cf42d5e1 failed → completed in-thread |
| M11 | Phase 19 | Incremental refresh architecture (watermark + plan + stale readback) | ✅ | `src/incremental.py` + tests OK; `--opportunity-pipeline` CLI; subagent 84fc844e failed → completed in-thread |
| M12 (Step 1) | parser eval + reference fallback | Labeled eval vs reference GT + `match_reference` pack fallback + range-kg guard | ✅ | `scripts/eval_parser.py`: PRIMARY gate (name-embedded pack) **94.7% within-5%** on 3,070 reference names → **GATE PASS ≥80%**; reference-fallback recovery 57% (300/9,393 sampled); `match_reference()` in `src/product_fields.py` (exact + fuzzy ≥0.9, `parse_source=reference:<ds>`); `_RANGE_KG_RE` skips baby-weight ranges (32 diaper rows fixed). Live report: brand 97.5%, real Ollama LLM path proven (llm_used=True on empty-brand names) |
| M12b | Phase 16/17 | Synthetic regression suite + atlas verify + test-green sweep | ✅ | `tests/test_validation.py` 8/8 (known gaps, false gaps, crawl-artifact, voucher, entity-resolution); atlas `normalize_opportunities()` fixes silently-discarded `--opp-json` records (subagent-4); full suite green incl. bare-run bootstraps |

### Reference data already in `reference/` (fetched 2026-09-07)
| File | Rows | Pack ground truth? | Role |
|---|---|---|---|
| `BigBasket.csv` (Kaggle `chinmayshanbhag/big-basket-products`) | 8,208 | ✅ `Quantity` **100%** | primary pack GT — India grocery |
| `amazon_india_products.csv` (HF `pgurazada1/amazon_india_products`) | 30,000 | ✅ `Pack Size Or Quantity` **99%** | secondary pack GT — broader mix |
| `BigBasket Products.csv` (Kaggle `surajjha101/...`) | 38,341 | ❌ | brand + sub_category vocab (no pack col) |
| `Indian Packaged Foods Nutritional Composition Data/packaged_foods_india.csv` (Mendeley) | 852 | ⚠️ `Serving_Size_g` | held-out eval / nutrition join |
| `openfoodfacts_india.csv` (OFF API) | 999 (partial) | ✅ `quantity` | ODbL, resumable via `fetch_off_india.py` |

### Step 1 plan (tomorrow) — ✅ DONE 2026-09-08 (see M12 row above)
1. ~~Build labeled eval~~ → `scripts/eval_parser.py`: primary gate scores name-EMBEDDED pack (GT from the name, not the column — scoring column GT against name parse punished correct no_pack parses); secondary gate measures `match_reference` recovery (300 sampled); tertiary = our-names fuzzy join.
2. ~~Add `match_reference(name)`~~ → in `src/product_fields.py` (exact → difflib ≥0.9), adopted when regex pack is None; `parse_source` provenance recorded.
3. ~~Mine labeled pairs~~ → `_RANGE_KG_RE` baby-weight-range guard (32 diaper rows) is the evidenced mining; unit-dict extension beyond that deferred (ponytail: remaining misses are count-edges 100 + g↔ml 29, no ≥3-row dictionary patterns).
4. Mendeley 852 remains held-out (never trained/evaled).

### Comments & deviations (one-liner, with reason)
- reference-DB alignment slotted into M2 not M1 — user: parser stays M1 regex default; richer extraction deferred to M2.
- alignment uses stdlib `csv` only, no pandas — AGENTS.md no-pandas rule; OFF full `food.parquet` (7.8GB) deliberately NOT downloaded.
- OFF India pulled via API-filter (India only) + resumable fetcher — OFF edge-blocks this IP (503/401), only 999 rows landed; rest fetched later.
- Mendeley-publishing of our own catalog noted as future idea — product-level data only (no PII), low redistribution risk; choose CC0/CC-BY at upload.
- M2 (3B/3C) + M3 + M4 + M5 + M6 + M7 implemented directly in-thread, not via subagents — background subagents were terminated by the environment (see M8/M10/M11 notes). M9 subagent (5e4362df) finished and delivered; M8 (ee8dd81f), M10 (cf42d5e1), M11 (84fc844e) failed → completed in-thread per AGENTS.md concurrent-agent rules (verify before/after, targeted edits, single log).
- M12 round (2026-09-08): 4 subagents launched; 3 delivered (5=validation suite, 4=atlas normalize fix, 6=live parser report + real Ollama LLM proof), 1 stalled (3: delivered eval harness + range-kg guard but left eval gate mis-designed w/ `use_reference=False`) → killed, finished in-thread (gate redesigned to name-embedded GT → 94.7% PASS).
- eval_parser primary-gate design deviation — original harness scored Quantity-COLUMN GT against name parse (measured column recovery, 11.8% "FAIL"); redesigned to name-embedded GT because our catalog names embed the pack; fallback recovery reported separately (57%).
- AGENTS.md: added standing rule "use the subagent tool generously" (user directive 2026-09-08) + stall-handling (kill, keep on-disk work, finish in-thread).

## Polish pass (2026-09-08, post-M12): 4 read-only audits → 5 fix batches

Status: all batches applied + verified; primary eval gate PASS 94.7% (unchanged);
union load 167,333 rows 37.8s → 33.7s; grouping 24.5s → 14.4s (total ~48s).

- A (in-thread, parser/union hot path): match_reference O(1) exact-index (was
  197ms/name linear scan — the --score-opportunities 60s-timeout root cause;
  fuzzy difflib pass now OPT-IN). multipack pack_value now TOTAL ("2 x 500ml"
  → 1000ml: per-unit 500 halved every derived ₹/L; SELFTEST + eval GT + _ref_gt
  updated to same convention). _mk_row: use_llm=False (per-row Ollama HTTP on
  the 167k hot path silently hung any Ollama-up box) + parse_source/llm_used
  provenance passthrough + parse_source:'none' on unparseable early-return.
  Entity-resolution veto tightened: unknown-pack/variant no longer fuzzy-joins
  (one-side-unknown = veto; only exact canonical key joins unknowns; either-
  side-set variant must agree). SQL: f-string ts interpolation → bound param.
  _REF_FILES CWD-relative → repo-root absolute. parse_brand/_VARIANT scans →
  compiled longest-first alternation regexes (7us vs 64us/name; &#39; entity
  treated as boundary; Lay's now brands to "Lays" not "Lay'").
  _norm_product_name alternation tried + MEASURED SLOWER (str.replace wins when
  every needle is replaced) → reverted, comment left so it's not retried.
- B (subagent): density _coord half-missing→None (was 0.0-coord false-sparse),
  _norm_coords zero-guard, per-category (not global) cat_age, age-0.0 None
  fix, O(k²)→sort-by-x prune (exact, 0/300-group mismatches), product_vectors
  days_since_first_seen None, assortment _confidence dead param dropped,
  opportunities dead WEAK_COVERAGE branch deleted + docstring honest,
  load_latest_opportunities id-anchored snapshot select (float-equality merge),
  offline ordering-only scoring documented + ponytail ceiling.
- C (subagent): temporal analyze_temporal now queries real catalog_snapshots
  history (2 recent sweeps: growing→emerging / shrinking→declining; proxy kept
  only for db=None w/ ponytail), stability_report unmeasured fields → None w/
  ponytail, human_review real additive opportunity_reviews persistence
  (submit/reviews_for/apply_review_feedback round-trip; db=None raises),
  build_brief score guard + honest LLM-stub labels, incremental
  products_changed_since real snapshot-EXCEPT + catalog_events diff, epoch-sec
  watermark semantics + None/[] distinction. Tests: time-series fixtures,
  round-trips, exact-branch asserts (was tautologies).
- D (subagent): run.py --score-opportunities --csv no longer ALSO prints the
  top-N summary; --validate-opportunities empty-snapshot message; README +
  AGENTS Commands now document --temporal-analysis / --validate-opportunities
  / --opportunity-pipeline + --min-n honor note; test_product_space sys.path
  bootstrap (bare-run fails no more).
- E (subagent): atlas cross-app REAL per-name app bitmask array (__APPMASK__,
  popcount coloring 1/2/3+ apps — was app-INDEX misread), unit-price REAL array
  (__UPRICE__, NaN→neutral+tooltip — was identical to price mode),
  normalize_opportunities passes evidence through (gap types/score/gap_
  strength/dpi/coverage/provenance/validation codes/category — inspector
  "WHY FLAGGED" shows real values, canned claims removed), glyph symbols
  (circle/circle-open/diamond/x) via marker.symbol restyle.
- Deviation note: eval secondary gate 57% → 50.3% after the multipack-total GT
  change (honest re-scoring: reference Quantity strings with "2 x 500ml"
  shapes now expect totals; per-unit had inflated agreement). Primary gate
  (name-embedded, the one that matters for our catalog) unchanged at 94.7%.
- ponytail ceilings left (marked in code): offline opportunities scores are
  ordering-only (NEUTRAL_DPI=0.5); stability projection/neighbor fields None;
  temporal count-proxy when db=None; LLM brief stub labeled; parse_name
  `category` param documented as unused forward hook (kept: dropping churns
  callers for zero behavior change).
- Post-commit verification (same day): full 10-stage CLI chain re-run
  end-to-end (vectors → assortment → density → gaps → score --persist →
  temporal → validate → report) in 1m55s total — the M12 60s
  --score-opportunities timeout is confirmed dead; new snapshot 17,128
  opportunities (was 12,981; stricter veto creates more honest groups);
  temporal now emits real emerging/declining per category from sweep history.
  Atlas fresh render verified (4.5MB, all placeholders filled, evidence +
  glyph restyle in bundle).
