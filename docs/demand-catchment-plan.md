# Demand Radar — Per-Store Catchment Radius (Phase Plan)

Purpose: each darkstore's `catch_radius_m` derived from the spread of anchor points that resolve to it (`locality.py` `entry["points"]`), without a new crawl. Enables future `--demand` catchment-weighted OOS analysis.

Status: not implemented (planned). Last verified reference: smoke test `bash-2` (09-04) produced 4 distinct stores (1295147/Andheri, 1404958/Goregaon, 1392421/Borivali, 1403051/grid r6c4) with known anchor-point spreads.

## Phase 1 — Compute radius (locality.py, no crawl change)
In `LocalityMapper.discover_for_app` (after `seen.setdefault` loop, before `_export`):

```python
# Per-store catch_radius_m = max haversine distance from representative
# point (first point in entry['points']) to all other discovered points.
for entry in seen.values():
    pts = entry.get("points", [])
    if len(pts) < 2:
        entry["catch_radius_m"] = 0
        continue
    rep_lat, rep_lon = pts[0]["lat"], pts[0]["lon"]
    max_dist = max(
        haversine(rep_lat, rep_lon, p["lat"], p["lon"]) for p in pts[1:]
    )
    entry["catch_radius_m"] = round(max_dist, 1)
```
Requires a `haversine` helper in `src/locality.py` or reuse from `src/geo.py` (`Corridor`). No new dependency.

Verification: `python3 -m src.locality` (offline self-test) passes; locality JSON contains `catch_radius_m` per store.

## Phase 2 — Update locality JSON export (`_export` / output format)
In `_export`, include `catch_radius_m` in each store dict:

```json
{"store_id":"1295147","label":"...","points":[...],"eta_min":14,"catch_radius_m":~820}
```

No DB schema change required for Phase 1+2. Radius is a derived property of the mapping snapshot, not a new crawl.

## Phase 3 — Optional DB backfill (`deals.db` darkstores, additive column)
If `--store-inventory` or dashboard DB view needs the radius persistently:

- `CREATE TABLE IF NOT EXISTS ...` / `ALTER TABLE darkstores ADD COLUMN catch_radius_m REAL DEFAULT 0;` (additive, per AGENTS.md schema rules).
- Backfill: for existing rows, set 0 (unknown historical spread) or compute retroactively from `exports/locality_*.json` if preserved.
- `Store.upsert_darkstore()` in `src/store.py`: include `catch_radius_m` parameter.

Skip Phase 3 if JSON-only is sufficient (ponytrail minimal preference: Phase 1+2 sufficient).

## Phase 4 — Documentation + future `--demand` usage
- `AGENTS.md`: document radius derivation ("derived from discovered anchor spread, not warehouse GPS; 0 = single-point catchment; multi-point = spread in meters").
- `README.md`: note that `catch_radius_m` is derived; no extra crawl.
- Future `--demand`: optional catchment-weight logic (`OOS intensity weighted by radius / area overlap`) — out of scope here.

## Verified reference data (09-04 smoke, bash-2)
| Store | Points (discovered) | Expected radius (approx, manual) |
| 1295147 | Andheri, grid r7c4 | ~800 m (lat diff ~0.00059 ≈ 65m; lon diff ~0.008 ≈ ~830m at 19.12°) |
| 1404958 | Goregaon, grid r7c3 | ~1,100 m (lat diff ~0.00051 ≈ 56m; lon diff ~0.007 ≈ ~770m) — spread is mainly east-west |
| 1392421 | Borivali (single) | 0 |
| 1403051 | grid r6c4 (single) | 0 |

Note: `haversine(rep, other)` must handle the actual 2-point spread correctly (not just approximate from lat diff). Implement in Phase 1.

## Constraints / failure modes
- Radius represents DISCOVERED anchor spread (catchment proxy), not warehouse GPS. Consistent with current design (coordinate = neighborhood reference point).
- No new crawl; no new dependency; no schema breakage (Phase 1+2). Phase 3 optional.
- Concurrent-agent: edit targeted (`locality.py`), verify `run.py --check` and `python3 -m src.locality` after.

---

# Feature 2 — Brand-level demand report (portfolio item)

Purpose: package Demand Radar findings per BRAND (e.g. "Amul · Borivali–Andheri corridor") as a self-contained markdown report for portfolio use. Decision (09-04): **portfolio first**; actual brand-outreach packaging is a later phase behind the same engine. Audience framing decides polish level — portfolio = showcase the pipeline, not a deliverable.

Data grounding (verified 09-04 sqlite survey of deals.db):
- `watchlist`: 92,168 SKUs; first-word tokens carry strong brand signal (AMUL 1828, CADBURY 1024, BRITANNIA 1020, TATA 952, NOICE 1138, GODREJ 495, NESTLE 469) but ALSO noise (THE 1882, WHOLE 775, PAPER 545, ORGANIC 467, RAW 396) and multi-word brands whose true name spans tokens (BASKIN→Baskin Robbins, MOTHER→Mother Dairy, KWALITY→Kwality Wall's).
- `oos_events`: 208 (Blinkit, 13 stores, window 08-24→08-31; 163 open = prober stopped). Instamart depth is 1 store/188 obs only — the fresh corridor stores (1295147 etc., mapped 09-04) have NO stock_obs yet.
- Therefore: a portfolio demo report is credible on BLINKIT data today; an Instamart-flavored demo needs `--build-watchlist` + a few `--demand` cycles on the new corridor stores first (prerequisite, not code).

## Phase B1 — brand attribution (`src/brands.py`, ordered-rules classifier)
Same pattern as `src/categories.py` (ordered keyword rules, zero deps):
- `BRANDS`: ordered list of (regex-or-multiword-token, canonical brand) — multi-word first (Baskin Robbins, Mother Dairy, Kwality Wall's, Go Zero), then exact first-word dictionary (Amul, Cadbury, Britannia, Tata, Nestle, Godrej, Epigamia, Sunfeast, Boat, Mars, Farmley, Noice, …).
- `STOP_TOKENS` (the, whole, paper, organic, raw, red, go, fresh, …) → never a brand match; fall through to `None`.
- `brand_of(name) -> str|None`. One runnable self-check (`python3 -m src.brands`) asserting the noise tokens return None and multi-word brands resolve — smallest check that fails if the rules break.
- NOT auto-scan of every SKU up front: attribution happens at report time over the query's SKU set (lazy, bounded).

## Phase B2 — per-brand rollups (pure sqlite, no crawl)
`src/brand_report.py` functions (mirror `src/demand.py`'s pure-rollup style):
- OOS hours per store (sum durations of brand-SKU oos_events), hour-of-day onset histogram (existing `demand.py` logic, filtered by brand SKU set).
- Worst-store table (fill gaps), catalog delistings (`catalog_events` where SKU is brand's), price/MRP drift from `price_obs` (reuse `Store.usual_price`), ETA context from `stock_obs.eta_min`.
- SKU set = watchlist names matching `brand_of(name) == brand` (join via `watchlist` table; stock/oos keyed by sku_key).

## Phase B3 — report generator + CLI
- `python3 run.py --brand-report --brand Amul [--app blinkit] [--store ID] [--out exports/]`
- Writes `exports/brand_report_<brand>_<stamp>.md` (gitignored like ai_explain; same strict-filename download path if dashboard ever serves it — reuse the `/ai/report/<file>` pattern).
- Report header (MANDATORY, non-negotiable framing): "Stock-out-intensity PROXY, not sales volumes" + data window + store coverage + methodology one-pager (anchor grid → per-store prober → debounced OOS). This keeps DEMAND_RADAR.md known-limit #5 (research-only, don't resell) intact — a portfolio item shows the pipeline without becoming a data reseller.
- Sections: headline stats → per-store OOS table → onset heatmap (ASCII/markdown grid, reuse demand.py hour×SKU shape) → delistings → price drift → "what this method can/cannot say".
- `run.py` flag list + AGENTS.md Commands + README documentation per repo rules.

## Phase B4 (DEFERRED — only if portfolio succeeds) — outreach packaging
- Redaction of store IDs / darkstore coords, provenance page, disclaimer hardening, per-brand cover letter. Same engine, `--audience outreach` flag. Deliberately not built now (ponytail: no speculative polish).

## Sequencing / prerequisites
- Independent of catchment Phase 1–4 (different consumers; both additive).
- Best demo order: run catchment Phase 1–2 first (radius enriches the per-store table in the SAME report), then B1–B3.
- Instamart demo requires fresh `--build-watchlist` on corridor stores (NO concurrent crawls — one store at a time per AGENTS.md rate-limit rule).

## Failure modes
- Brand misattribution (THE/WHOLE tokens, multi-word brands) → B1 stop-token list + multi-word-first ordering; self-check covers the known noise.
- Thin data = weak report (open-event storms from a stopped prober inflate "OOS hours") → report generator must note open events as "ongoing as of window end", never fabricate ended_at.
- Unbranded SKUs (generics, store brands) → `brand_of` returns None; report states coverage % honestly (e.g. "412 of 921 SKUs attributed").
