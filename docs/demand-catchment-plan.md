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

---

# Feature 3 — Per-app store-meta harvest (real coordinates + geofences)

Source: InfoSecWriteups article "How I Scraped Most Dark Stores in India" (Jatin Banga, 03-2026, https://infosecwriteups.com/how-i-scraped-most-dark-stores-in-india-blinkit-zepto-swiggy-instamart-ad939ff17af9 — fetched via monid 09-04). Author scraped a NATIONAL census of darkstores via direct API probing; the gold fields below ride the SAME responses our browser-intercept already sees but our `extractMeta()` walker ignores because they're not ID-shaped keys.

## What the article proves we're leaving on the table

| App | Field in intercepted responses | Meaning | Our extractor today |
|---|---|---|---|
| Blinkit | `promise_time_state.DistanceInMeter` (layout/feed) | exact ROAD distance in metres from probe point to store | ignored |
| Zepto | `storeDetailsResponse` → `latitude`/`longitude`, `name`, **`servicableGeofence`** (sic — misspelled in API) | real store coords + delivery-boundary polygon | ignored |
| Instamart | `storesInfo` array → coords + operational status | real store coords | ignored |

Key correction to our earlier belief: "the platform never exposes the warehouse's physical lat/lon" is FALSE — Zepto returns it with the geofence in one structure. This directly upgrades Feature 1: on Zepto the catchment radius becomes the app's OWN polygon, not a derived proxy.

## Design: split the META layer, NOT the crawler (ponytail cut)

The user instinct "separate functions per app" is right, but the correct seam is the store-metadata extractor, not the whole crawler. Rationale for keeping the rest generic:

1. Generic product walker = resilience to payload reshuffles (battle-tested across 7 apps; per-app product parsers would triple the regression surface for zero product-data gain).
2. Direct-HTTP per-app clients (article's approach: curl_cffi + forged Android headers + proxy pools) violate our documented anti-bot constraints: Zepto/Instamart sit behind AWS WAF with session-bound tokens (see AGENTS.md APK notes) — browser-harvests-signed-responses is the correct architecture. Blinkit has no WAF but consistency wins.
3. Their mission (national census, 300K-point WorldPop-filtered grid) is a different product from our corridor demand loop; we adopt their FIELD discoveries, not their scale strategy.

## Phase C1 — harvest hooks in pw_catalog.js (thin, additive)

Three small per-app functions + dispatch in `extractMeta()` (each ~10 lines, merged into `store_hint`):

- `harvestBlinkitMeta(node)`: on `promise_time_state`, read `DistanceInMeter` → `store_hint.distance_m`.
- `harvestZeptoMeta(node)`: on `storeDetailsResponse`, read `latitude`, `longitude`, `name`, `servicableGeofence` → `store_hint.store_coords` + `store_hint.geofence` (array of [lat,lon] pairs; store VERBATIM — it's the app's own boundary).
- `harvestInstamartMeta(node)`: on `storesInfo`, read coords + operational status per store.
- New keys are additive to the final `console.log` JSON (line ~1374); `base.py` `_browser_catalog_full` meta passthrough extended to carry them; `locality.py` persists coords (optional: only when present — don't fabricate).

Schema (additive, per AGENTS.md rules): `darkstores` gains optional columns `store_lat`, `store_lon`, `geofence_json` (TEXT, nullable) — backfill-safe; existing rows keep NULL until re-probed.

## Phase C2 — Blinkit micro-grid convergence (50m store accuracy)

Article's trick, adapted to our session model (NO new crawl mode): the layout/feed `DistanceInMeter` tells us exact road distance from our anchor to the store. After `--map-locality` resolves a store ID at an anchor, run a second pass: generate an 11×11 micro-grid around the anchor (~121 points in one browser session via the existing visit-queue machinery), take the minimum reported distance, converge. This is the same machinery as the mirror-pagination pattern — reuse the visit queue, one session per store (invariant preserved).

Alternatively (cheaper): single-session iterative refinement — binary-search the distance field from 4 compass points, ~20 visits per store instead of 121. The distance is exact (metres, road distance), so 4 probes triangulate to <100m; 8 probes to ~50m. Choose at implementation time based on rate-budget.

## Phase C3 — geofence → catchment boundary (unifies with Feature 1)

When `store_hint.geofence` is present (Zepto), compute catchment metrics from the polygon:
- `catch_radius_m` = max distance from store coords to polygon vertices (replaces derived anchor-spread on Zepto).
- Store the polygon in the locality JSON export; dashboard `/location` map can draw it later (the pins already exist there).
- Feature 1's anchor-spread radius stays as the FALLBACK for apps without geofences (Blinkit/Instamart).
- Anchor→store assignment can be validated: does the anchor fall inside the serving store's geofence? Cheap sanity check on mapping honesty.

## Phase C4 — verification + docs

- `DSH_BODY_DIR` re-discovery pass on one Zepto run to CONFIRM `storeDetailsResponse` appears in OUR intercepted traffic (article saw it on the authenticated get_page endpoint — our guest sessions may see a subset; verify before relying). Same for Blinkit `promise_time_state` and Instamart `storesInfo`.
- Honest ceiling note: article notes Zepto's deep store data needed an AUTHENTICATED endpoint (Bearer token from a logged-in session) — we do NOT log in (no fake accounts invariant). So C1 hooks may yield partial data on Zepto; the unauthenticated serviceability storeId (what we use today) still works. Document whatever we actually capture vs. the article's authenticated reach.
- AGENTS.md per-app quirk notes + README + this doc updated with verified findings.

### C4 ZEPTO RESULT (VERIFIED 09-04, guest sessions, no login, DSH_BODY_DIR probes at 2 anchors)

Claims tested against OUR traffic (search-route probes, bodies dumped; 8 files incl. `bff_gateway.zepto.com/lms/api/v2/get_page`):

| Article claim | Our guest session | Verdict |
|---|---|---|
| `storeDetailsResponse` rides `get_page` | endpoint FIRES unauthenticated in our guest browser session; structure present at top level | ✅ VERIFIED — no login needed for identity/status |
| Real store `name` | `MUM-Gulmohar road` (Andheri 19.119,72.846) vs `MUM-Borivali West` (Borivali 19.230,72.856) — genuinely anchor-localized | ✅ VERIFIED — better store labels than our current `storeid` counts |
| Precise `latitude`/`longitude` | keys ABSENT from guest `storeDetailsResponse` | ❌ login-walled (matches article's auth note) |
| `servicableGeofence` polygon | key PRESENT but EMPTY ARRAY `[]` in guest sessions | ❌ login-walled — geofence is the authenticated payload |
| Operational status | `isOnline`/`takingOrders`/`isActive`/`isLive`/`phase`/`type`/`openTime`/`closeTime` all present | ✅ VERIFIED |

Guest `storeDetailsResponse` full shape (both probes): `city`, `cityId`, `closeTime` ("20:30:00"), `estimatedLaunchDate`, `id` (UUID, matches `storeServiceableResponse.storeId`), `initiateSdkNewFlow`, `isActive`, `isFullNightDeliveryEnabled`, `isLive`, `isOnline`, `issueAtStore`, `name`, `openTime` ("00:30:00"), `phase` ("LIVE"), `raining`, `servicableGeofence` ([]), `standStillMode`, `takingOrders`, `type` ("RETAIL_STORE").

**Bonus discovery the article missed**: `storeServiceableResponseV2` returns an ARRAY of stores with `storeConstruct` — every anchor is served by a PRIMARY + SECONDARY store (Andheri: 94036a35 primary + 0e8c4509 secondary; Borivali: 551d5b20 primary + b26ca2be secondary; V1 response carries `secondaryStoreIds` too). This is failover coverage data — a catchment-overlap signal — at zero extra cost. Also: search responses carry per-variant `storeId` (a 3rd/4th UUID set, e.g. b4dc8d65 count 30 vs get_page's store) — variant-level fulfillment differs from the home-page store binding; treat the get_page store as canonical for anchor→store mapping.

**Revised C1 scope for Zepto** (honest ceiling): harvest `name`, `id`, `isOnline`, `takingOrders`, hours, and the V2 primary/secondary constructs — NOT coords/geofence (login-walled; we don't log in). C3 geofence unification therefore DROPS for Zepto; Feature 1's anchor-spread radius remains the catchment proxy for all apps. Blinkit `promise_time_state.DistanceInMeter` and Instamart `storesInfo` still UNVERIFIED (next C4 runs, when a crawl slot is free).

## Constraints / invariants preserved
- One browser session per store per sweep (micro-grid C2 runs INSIDE the existing visit queue).
- No forging, no direct-HTTP clients, no proxy-pool national census (rate-limit + research-scale rules).
- No login (Zepto authenticated endpoint stays off-limits; document the gap honestly).
- Schema additive only; existing `darkstores` rows never fabricated (NULL coords until re-probed).

## Sequencing
- C4 verification FIRST (prove the fields appear in our traffic before building hooks), then C1, then C3, then C2 last (optional accuracy polish).
- Feature 1 (catchment) proceeds independently; on Zepto C3 supersedes its derived radius when geofence data exists.
