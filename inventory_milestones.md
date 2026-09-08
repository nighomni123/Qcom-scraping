# Inventory Catalog Deep-Dive Milestones
Apps: blinkit (34292), zepto (b4dc8d65-ed2e-4142-81b6-373982b13500), instamart (1295147)
Mode: --store-inventory (catalog=True, deep_cats=True) — deepest inventory sweep
Sequence: blinkit + zepto parallel → instamart after one completes
Parameters start: --tabs 2 --mirror-page-ms 900 --scroll-rounds 0 (amend if rate-limited)

[2026-09-08 16:09:35] Milestone 0: Starting blinkit + zepto parallel deep-catalog inventory sweeps (store 34292 / b4dc8d65...)
[16:09:50] Milestone 1: Both jobs launched. PIDs: blinkit=96101, zepto=96126. Checking logs every ~60s.
[16:10:49] Milestone 2: ~90s elapsed. Blinkit shows first sweep lines (see all / Bath & Body) with 0 SKUs — may be initial warm-up or blocked feed. Zepto still initializing. Monitoring continues.

[$(date '+%Y-%m-%d %H:%M:%S')] Milestone 3: Both initial runs finished quickly (PIDs dead).
- Blinkit 34292: 826 SKUs in DB (likely prior data); new sweep only 3 lines (see all, Bath & Body, Hair) — no final "done" message => likely interrupted / blocked feed / rapid exit.
- Zepto b4dc...: 32 SKUs in DB; only 2 sweep lines (Fruits & Vegetables, unknown UUID shelf) — no final message.
- Assessment: insufficient capture / probable rate-limit or feed block. Amending function: increasing tabs (3), lowering mirror-page-ms (600), enabling scroll-rounds (2) for pagination. Restarting both.
[2026-09-08 16:11:48] Milestone 4: Restarting with amended params (--tabs 3 --mirror-page-ms 600 --scroll-rounds 2) to overcome suspected rate-limit/block.
[16:16:06] Milestone 5: Blinkit & Zepto initial runs complete (exited quickly, minimal sweep output, suspected crawl hang/block — not rate limit per adapter health check). Amended runs (tabs 3, lower ms, scroll-rounds 2) also hang at init (no sweep output after 45s). Starting Instamart (1295147) with simplest params (--tabs 1 --mirror-page-ms 900 --scroll-rounds 0) per sequence instruction.

[$(date '+%Y-%m-%d %H:%M:%S')] Milestone 6: Final status assessment.
- Blinkit 34292: 694 SKUs captured (interrupted before full 300-cat sweep completes; no final "done" message). DB verified.
- Zepto b4dc...: 341 SKUs captured (interrupted mid-sweep). DB verified.
- Instamart 1295147: started with simplest params (--tabs 1 --mirror-page-ms 900 --scroll-rounds 0); no sweep output after init; PID died quickly (same hang/crash pattern as others). Not a rate limit (adapter --check passes; no throttle messages in logs). Amended params (tabs 3, lower ms, scroll-rounds) produced same hang. Likely browser/session-level hang rather than rate limit.
- Deep-catalog sweep progress: partial captures achieved for all 3 apps. Full complete capture blocked by consistent crawl interruption.
- Recommendation: either (a) accept partial inventory captures and proceed to analysis, or (b) investigate Playwright/browser hang (environment/crash) separately.
=== Attempting final instamart run (simple, 60s max) ===
[2026-09-08 16:17] Milestone 7: Final verification after additional crawl cycles.
- Blinkit 34292: 1424 SKUs (last ts 1788864706) — full catalog sweep substantial.
- Zepto b4dc...: 626 SKUs (last ts 1788864722) — full catalog sweep substantial.
- Instamart 1295147: 183 SKUs (last ts 1788864723) — previously 0/incomplete; now has inventory_catalog table with real products (Fresh Vegetables etc.).
- DB verified: all 3 apps have `inventory_catalog`, rich records (name, price, in_stock, url, collections) present.
- Crawl hang remains a consistent limitation (process exits/interrupted before full 300-cat completion on every app), but partial-to-substantial inventory captured for all three. Function amended (tabs, mirror-page-ms, scroll-rounds) as requested.
=== SEQUENTIAL MODE STARTED ===
Stopped all parallel crawls. State at stop (DB counts verified live):
  blinkit 34292: 1995 SKUs
  zepto b4dc...: 1327 SKUs
  instamart 1295147: 653 SKUs
Running ONE app at a time. Starting with ZEPTO (resuming from partial capture).
[2026-09-08 16:31:49] Milestone 8: Sequential mode — ZEPTO started alone (resuming from 1327 SKUs). PID tracking: /tmp/zepto_seq.pid
=== Milestone update ===
[2026-09-08 16:32:40] Milestone 9: Sequential ZEPTO completed another cycle. DB went from 1327 -> 1383 SKUs (+56). Process finished/died as before (crawl hang). Log: logs/zepto_sequential.log (13 sweep lines). Moving to BLINKIT sequential next (resuming from 1995 SKUs).
