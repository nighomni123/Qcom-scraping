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
   (`src/categories.py`; dashboard endpoints `/searches`, `/categories`),
   plus proactive keyword watches: `/watch <product>` stores a chat-level
   watch and WatchPusher (`src/tgbot.py`) pushes Telegram pings whenever a
   search result or crawl batch matches (rate-capped); `/digest` replies with
   today's cheapest find per category + Demand Radar DPI top-5.
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
- **The repo is git-managed** (branch `main`, baseline `b7f3502` = verified
  merged state of all workstreams). `git status` must be clean when you start;
  commit after every verified milestone with a descriptive message. Never
  force-push or amend published history — the other agent builds on HEAD.
  If the tree contains modifications you did NOT make, stop: run the verify
  suite, `git diff` them, and read AGENTS.md/README changes before proceeding.
- Don't run two long crawls (`--demand`, `--map-locality`, `--build-watchlist`,
  `--store-inventory`, `live_sweep.py`) simultaneously against the same app —
  rate-limit bans hurt both workstreams. Coordinate timing instead. Observed
  08-24: an inventory sweep started while `--demand` was running got its
  fresh sessions throttled into empty probes (stores resolved, products=0),
  while the same sweep run alone captured 200+ products per probe.

## Repo map

```
run.py                  entrypoint; ALL CLI flags live here (--check --demo
                        --search --bot --ui --once --map-locality
                        --build-watchlist --demand --store-inventory; flag
                        args parsed by _flag_* helpers)
config.yaml             every tunable; secrets go in .env only
codes.yaml              delivery fees + offer codes (user-editable)
GETTING_STARTED.md      end-to-end first-run guide: what to run in what
                        order + how to interpret each output
GLOSSARY.md             plain-English index of ALL jargon/short forms —
                        link new contributors here before the other docs
tools/pw_catalog.js     THE crawler: real app in headless Chromium, intercepts
                        signed API calls; deep-sweep visit queue (categories +
                        search terms; --skip filters category labels — the
                        demand.skip_categories milk de-biasing knob); --pre
                        pre-visits gated verticals before the target page
                        (Blinkit tobacco shelf, "URL::LABEL|..." pairs);
                        location seeding + request rewrite;
                        DSH_BODY_DIR raw-body dump hook
src/
  orchestrator.py       glitch-monitor loop (jitter, off-peak speedup)
  locality.py           phase 1: anchor grid + darkstore discovery/clustering
  watchlist.py          phase 2: per-store SKU probe set builder
  prober.py             phase 3: stock_obs loop + debounced oos_events machine
  demand.py             phase 4: DPI rollups, hour×SKU onset heatmap, ETA
                        curves, CSV export (pure functions over sqlite)
  categories.py         keyword product-category classifier (ordered rules)
  inventory.py          per-app darkstore inventories near the machine's real
                        location (public-IP derived, no spoofing) into separate
                        inventory_<app>.db files (--store-inventory); runs
                        LocalityMapper in capture_products mode so each DB
                        also holds the probes' products (stock_obs source=
                        'inventory' + categorized price_obs)
  store.py              sqlite schema + all persistence helpers
  ai_assist.py          OPTIONAL LLM advisor behind the dashboard AI panel
                        (/ai/*): result explanations, whitelisted demand-probe
                        tuning, product-focus staple_queries; OpenAI-compatible
  geo.py                corridor anchors + store resolver (glitch monitor)
  adapters/             blinkit / zepto / instamart / amazon_now / bigbasket /
                         jiomart / dmart / amazon / flipkart / trackers / demo;
                        base.py holds the browser machinery
  detect.py alert.py honey.py events.py dashboard.py search.py pricing.py
                        tgbot.py miniyaml.py (stdlib YAML fallback)
exports/                locality mapping JSON (rotation pools per store)
docs/                   research reports: QuickCommerce API vetting + Zepto/Swiggy
                        APK reverse-engineering findings (source .apkm files live in
                        apks/, gitignored — too large to commit)
scripts/live_sweep.py   one-shot real-glitch hunt
deals.db                everything: price_obs, alerts, darkstores, watchlist,
                        stock_obs, oos_events, searches, search_results,
                        keyword_watches
```

## Commands

    python3 run.py --check                 # config + adapters sanity
    python3 run.py --demo                  # offline pipeline test (injected glitch)
    python3 run.py --map-locality [--apps blinkit,zepto] [--max-points N]
    python3 run.py --build-watchlist [--apps …] [--store ID]
                                    [--max-per-store N] [--max-queries N]
                                    # streams per-visit progress live: store
                                    # (i/N) + label + queued-visit scope, then
                                    # one [sweep] line per visit with cumulative
                                    # SKU count — watch in the terminal or
                                    # Features ▸ Build watchlist ▸ log; mute
                                    # via anti_block.stream_progress: false
    python3 run.py --demand [--once] [--apps …] [--store ID] [--max-terms N]
    python3 run.py --demand-report [--store ID] [--csv]  # DPI table + heatmap summary
    python3 run.py --qc-status [--apps blinkit,zepto,instamart]
    python3 run.py --store-inventory [--apps …] [--lat X --lon Y]
                    [--radius-m N] [--max-points N]
                                    # per-app darkstore inventories around the
                                    # machine's REAL approximate location (public
                                    # IP; no spoofing) -> inventory_<app>.db,
                                    # incl. captured products (stock_obs
                                    # source='inventory' + price_obs). Run
                                    # ALONE — no concurrent crawls (see rules).

Dashboard (`--ui`, http://127.0.0.1:8787) endpoints: `/status /categories
/searches /search/<id>` (bot analytics), `/demand /heatmap?store=
/eta /qc` (Demand Radar, DB-backed — live probing stays in `--qc-status`),
`/db` + `/db/<db>/<table>` (read-only browser over ALL repo sqlite files:
deals.db + inventory_*.db), `/features` + `/features/<id>/start|stop|log`
(spawn/terminate any repo feature — monitor loop, bot, --demand, demo,
inventory sweeps… — as managed child processes) and `/location`
+ `/location/presets` + POST `/location/locality` | `/location/corridor`
(interactive working-area editor: regenerates `geo.corridor`,
`demand.locality` and `search.station` blocks in config.yaml in the existing
miniyaml-compatible style, validates with BOTH loaders before an atomic
write, previous file backed up to /tmp; changes apply when a feature
process is next started/restarted) and `/ai/status` + POST `/ai/explain`
`/ai/explain/followup` `/ai/methodology[/apply]` `/ai/focus[/apply]` (OPTIONAL LLM assistant,
src/ai_assist.py: explains Demand Radar results, proposes demand-probing
methodology changes and product-focus staple_queries. /ai/explain also
persists the FULL analysis as markdown to exports/ai_explain_<stamp>.md
(gitignored; GET /ai/report/<file> downloads it — strict filename pattern,
nothing else in exports/ is reachable) so long outputs never depend on the
panel. /ai/explain/followup answers follow-up questions about that analysis:
it re-reads the digest fresh (sqlite), grounds on the prior analysis held in
ai_assist.LAST_EXPLAIN (the client echoes its displayed text as fallback after
a dashboard restart), and APPENDS each Q&A block to the saved report file so
the download stays complete. Provider is any
OpenAI-compatible endpoint via config.yaml → ai: + AI_API_KEY in .env;
local Ollama works keyless. Suggestions are parsed then ENFORCED against a
whitelist with bounds in ai_assist.INT_PARAMS — only those demand: knobs
can be written, through the same validated writer as the location editor;
the LLM never runs crawls). Invariant: those children belong
to the dashboard process; STOP ALL /shutdown reaps them, so never
orphan-start crawlers outside it while a dashboard is up.

Telegram bot (`--bot`) commands: `/watch <product>` — persist a keyword watch
(`keyword_watches` table; bare `/watch` lists yours, `/unwatch <product>`
removes); every `--search` run, bot search, and monitor/demo crawl batch is
matched against active watches (`WatchPusher`, same token-overlap scoring as
search ranking) and pushed to the watching chat, capped by
`config.yaml → alert.rate_cap` pushes/hour + a 6h per-watch cooldown
(`last_alerted_ts`, restart-proof). `/digest` — today's cheapest find per
category from the searches archive + Demand Radar DPI top-5 (pure sqlite, no
crawling).

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
  from dumps instead of guessing. For gate/onboarding debugging set
  `DSH_DEBUG_DIR=/tmp/imdebug`: a run stuck at zero products dumps
  stuck.png + clickable elements + API status log there, and onboarding
  rounds record every CTA they considered.
- **Location is enforced, not assumed**: QC apps cache their serving location
  client-side and will silently serve a DIFFERENT CITY's store (Blinkit
  defaulted to Gurugram). `pw_catalog.js` seeds `localStorage.location`,
  `gr_1_lat/lon` cookies, drops the cached `merchant`, AND rewrites lat/lon in
  request paths/query/JSON bodies onto the target anchor. If you add a new app,
  replicate both layers. Verify via the `visibility`-style response (city must
  match the anchor).
- **Zepto specifics** (fixed 08-22 — don't regress): zeptonow.com 301s to
  www.zepto.com; the search route reads `?query=` (`?q=` loads a home shell and
  NEVER fires user-search-service/api/v3/search); prices arrive in PAISE and
  are normalized by `PRICE_DIVISORS` in pw_catalog.js; product name lives on a
  nested `product` object of each variant node; stock flag is `outOfStock`;
  home serves NO products for fresh sessions → `HEALTH_URL` overrides to the
  search route for health checks.

- **Blinkit Paan-corner structure** (traced 08-30 via raw-body dumps): the catalog is a
  **layout-engine tree** — `/v1/layout/tag_collections` lists every **grouping** (named shelf)
  with `collection_filters: [{l1_cat_id: [...]}]`. Paan corner (`hpc_paan corner`) leaves:
  **15119 'Cigarettes' → l1_cat 1948** (real cigarettes: Marlboro/Gold Flake/Classic),
  62439 'Cigar' → 3466 (cigars/cigarillos ONLY — don't confuse the two), 383144 'Lighters'
  → 7778, 11644 'Rolling Needs' → 1982, 127957 'Paan Masala' → 2517. URL shapes:
  `/cn///cid/<l0>/<l1>` (category page → `/v1/layout/listing[/widgets]/l0_cat/X/l1_cat/Y`),
  `/dc/<slug>/?collection_uuid=<b64>&collection_group_id=<gid>` (collection route to the same
  shelf; its `listing_widgets` call returns `is_success:false` for FRESH sessions — works only
  in browsers with prior state), `/prn/<slug>/prid/<id>` (product detail + related rail).
  KEY QUIRK: **Blinkit text search (`/s/?q=`) serves NO tobacco** — server-side curated to
  smoking accessories only (verified across 13 darkstores AND with sessions warmed by the shelf
  itself), while direct category/product URLs serve the full tobacco assortment **with no age
  gate**: the "appropriate age / not near schools" interstitial guards ONLY the banner-click
  path in the UI. Workaround shipped 08-30: `BlinkitAdapter.search()` pre-visits the cigarette
  shelf via `pw_catalog.js --pre "URL::LABEL"` on tobacco queries (with a wait-for-products +
  reload-retry loop — a fixed short sleep harvests 0), merges it with the text-search harvest,
  and `search.match_score(query, name, context)` counts the `pre:Cigarettes` shelf label toward
  query matching. The age gate is UI-only — do NOT automate clicking it; go straight to the
  shelf URL.
- **Zepto API signing** (verified 2025-08-30 via Playwright request capture — the
  js-reverse Observe/Capture equivalent, since the js-reverse/jshookmcp MCP isn't
  wired into this session and its bootstrap is Windows-only): every API call hits
  `bff-gateway.zepto.com` and is signed client-side per-request with
  `request-signature` (SHA-256 hex), `x-csrf-secret`, `x-xsrf-token`, `x-api-key`,
  plus `device_id`/`session_id`/`store_id` UUIDs and an `x-timezone` hash. The
  signing code lives in bundle chunk `89411-*.js` (the `XMLHttpRequest.open`
  wrapper) + `88682-*.js`. Our in-browser crawl harvests the **already-signed**
  responses, so **no forging is needed**; the signature binds to the session's own
  `device_id`/`session_id` (from the app's cookies), making this robust to signature
  gating. The only hard gate observed is IP-reputation (403 login wall — see
  Instamart note), not signature validation.

## Demand Radar invariants (do not break)

- **NULL ≠ OOS.** A failed parse/crawl records `in_stock=NULL`; it must never
  become 0. Scraper errors must not fabricate demand.
- **Debounce.** An `oos` event opens only after `demand.oos_debounce_snapshots`
  consecutive zero-reads; any 1-read closes it; nulls pause, not reset.
- **Streaks are restart-proof** — rebuilt from `stock_obs` history
  (`Store.trailing_oos_streak`) each sweep; `started_at` is the FIRST zero
  read, never the moment the threshold was crossed.
- **Vanished ≠ OOS.** An active watchlist SKU missing from `vanished_cycles`
  successful sweeps opens `kind='vanished'`, not `kind='oos'`. The same
  absence rule also RECONCILES stale `oos` events on SKUs outside the active
  set (08-30 zombie fix: search-passed SKUs that fell out of coverage held
  open events for days and inflated DPI): close at the LAST OBSERVED reading
  — durations never fabricate unseen time — and re-open as `vanished`.
- **Suspect cycles freeze the machine.** Canary queries returning only-OOS or
  a mass in-stock→OOS flip ⇒ record observations, open/close nothing.
- **One browser session per store per sweep.** Home → categories → searches
  inside a single page; never spawn per-SKU or per-term processes.
- **Stock/ETA are per darkstore, not per address.** Anchor grids discover
  catchments; probes hit each store once per cycle (rotate anchors across
  cycles later if needed).
- ETA is per-store/per-snapshot (`stock_obs.eta_min`) — there is no
  per-product delivery time in any app.

## Reverse-engineering toolchain (persistent, repo-independent)

Android/APK reverse-engineering tools are installed **outside this repo and outside
`/tmp`**, at `/Users/Mitesh Gada/revtools` (self-contained: bundles its own JRE 17, so
no system Java is needed). Use these for any APK/binary analysis task — including in
**other repos**:

- `jadx` 1.5.6 (DEX→Java decompiler) and `apktool` 3.0.3 (APK→smali + decoded
  manifest/resources) are on PATH via `~/.zshrc`. If PATH isn't picked up in a
  non-interactive shell, call them by absolute path:
  `/Users/Mitesh Gada/revtools/bin/jadx` and `/Users/Mitesh Gada/revtools/bin/apktool`.
- The `bin/` wrappers set `JAVA_HOME` + `JAVA_OPTS=-Duser.home=$HOME/revtools/.cache`.
  That redirect is **required** — without it the tools try to write to
  `~/Library/Application Support/...`, which the sandbox blocks ("Operation not
  permitted"). Do NOT invoke the bare `java -jar` of `jadx`/`apktool.jar`; always use
  the `bin/` wrappers.
- Full usage + bootstrap-from-scratch instructions:
  `/Users/Mitesh Gada/revtools/README.md`.

Worked example (Zepto consumer APK under `apks/`): the app side is gated by **AWS WAF**
(Mobile SDK `com.amazonaws.waf.mobilesdk`, native `NativeWaf` module); the WAF token is
delivered as the `aws-waf-token` cookie. The web `request-signature` / `x-csrf-secret`
/ `x-xsrf-token` / `x-api-key` signing lives in the **web** bundle (see the Zepto note
above), NOT the APK. The crawler's "harvest already-signed responses" approach remains
correct — do NOT forge the AWS-WAF token. Two follow-ons from the same analysis: the WAF
token is **session-bound** (solved per browser session by the app's own WAF SDK), so
rotating exit IPs under a live context invalidates it — each proxy identity needs a FRESH
browser context (Phase 5 constraint, see DEMAND_RADAR.md); and `assets/api_key.txt` in the
APK is an **Amazon LWA key, NOT Zepto's `x-api-key`** — a distractor, don't chase it.
Zepto hosts have no cert pinning (mitm capture is a viable fallback if web sessions ever
get gated harder). Full report: `docs/zepto-apk-reverse-findings.md`.

Worked example (Swiggy consumer APK `in.swiggy.android_4.115.1-1817` under `apks/`,
focused on **Instamart**): the app-side anti-bot is **AWS WAF too** — `com.amazonaws.waf.mobilesdk`
classes plus an interceptor reading `x-aws-waf-token` / `x-amzn-waf-action` /
`x-amzn-waf-rate-limit` (same family as Zepto). The native API is authenticated by an
**HMAC** scheme: `network/interceptors/b.smali` sets `x-swiggy-auth` (with `populateHMAC`
/ `CalculateMac` / `calculateX2s` machinery) plus `x-oztok` / `x-channel` / device headers;
`com.bureau.devicefingerprint` does fraud fingerprinting. Instamart is a **`webviewV2` +
`externalWidget` "instamart"** surface whose WebView shell is
`https://media-assets.swiggy.com/assets-aggregator/im_main_v3.json`; its tabs/sections are
**gandalf/widgets/v2** protobufs (Square Wire) served by the **Discovery** service
`disc.swiggy.com` (`evaluatePage` → `EvaluatePageResponse`), cached via `CacheEvaluatePageConfig`.
Native Instamart API paths: legacy `/api/v1/instamart{...}` (`{pageID}`, `product`,
`presearch`, `search-config`, `contextual_discovery`, `pass/activate`, `complimentary_item`,
`relay-vendor-order`) and the unified BFF `/api/v2/view?cartType=INSTAMART` (`IInstamartApi`).
`InstamartLatLngQueryInterceptor` injects `lat`/`lng` (or `latitude`/`longitude` under flag
`enable_im_lat_long_full_names`) into `/api/v1/instamart` requests. Crawler conclusion is
identical to Zepto: harvest already-tokened/signed responses in a real browser — **do NOT
forge** `x-aws-waf-token` or `x-swiggy-auth`. The app's `www.swiggy.com/instamart` SPA is
login-walled for our exit IP; `instamart.in` (the separate storefront) is the crawl target.
Full report: `docs/swiggy-apk-instamart-findings.md`.

Worked example (Blinkit `com.grofers.customerapp_18.23.0-280180230` under `apks/` — legacy
**Grofers** package; Blinkit is **Zomato-owned**): the app side has **NO AWS WAF** (no
`com.amazonaws.waf.mobilesdk`; only `mobileconnectors.remoteconfiguration`) — so Blinkit is the
*easiest* of the three QC apps on anti-bot (no WAF token to solve). Its API is gated by
**authentication, not a signature**: a **hardcoded Basic credential**
`Authorization: Basic base64("cde_external:uq8vGL99wd4RfP4ER33GxnU3")` plus an API-key header
`X-Zomato-API-Key` (Zomato infra) plus a session token. There is **no per-request HMAC**
(unlike Swiggy's `x-swiggy-auth` / Zepto's `request-signature`). On-device anti-abuse is only
**Google Play Integrity** (device attestation), which does not gate our browser crawl. The
home/feed "tabs"/sections are a **layout-engine**: `/v1/layout/feed` (and `/v2/layout/feed`)
returns the widget/section tree, per-screen layouts at `/v1/layout/{screen}`; static assets from
`cdn.grofers.com/layout-engine/v2/`. API host: `api3.blinkit.com` (also `api2.grofers.com`,
`api.blinkit.dev`). Location = Zomato `locationkit` lat/lng (same rewrite as other apps). The
embedded `cde_external` credential is a real extractable secret but the browser supplies it, so
**do NOT hardcode it** and **do NOT forge** anything — harvest already-authenticated responses.
Full report: `docs/blinkit-apk-reverse-findings.md`.

## Known limitations / TODO

- Zepto: WORKING since 08-22 (see Zepto specifics above). 24–30 products per
  search sweep, stock-stamped, store ids resolve per variant, ETA via the
  home redirect chain. Home page alone yields no products by design — one-shot
  probes therefore go through `PROBE_URL` (search route); continuous monitor
  crawls still hit home + honey searches only.
- Probe breadth: `Adapter.PROBE_URL` / `PROBE_TERMS` (base.py; overridden per
  app) make `probe_point()` product-bearing — Zepto probes its search route,
  Blinkit/Instamart fire a few staple terms in-session beyond the dairy-first
  home carousels. Deliberately NOT wired into the continuous crawl loop.
  As of 08-29 BlinkIt/Zepto `PROBE_TERMS` also include paan-shop / tobacco /
  convenience SKUs (`paan`, `cigarette`, `gutkha`, `pan masala`, `tobacco`,
  `condom`, `mukhwas`) so `--store-inventory` captures non-food categories, not
  just groceries — verified against QuickCommerce API's paan coverage.
  CAVEAT (08-30): on Blinkit those tobacco search terms yield only smoking
  ACCESSORIES (the text search serves no tobacco — see the Paan-corner quirk
  above), so q:cigarette watchlist captures are lighters; real Blinkit tobacco
  coverage in quick price search comes from the `--pre` cigarette-shelf merge
  in `BlinkitAdapter.search()` instead. Deep sweeps could adopt the same
  `--pre` visit later if tobacco stock-outs should be tracked.
- Instamart: targets **instamart.in** (NOT `www.swiggy.com/instamart`). The
  swiggy.com SPA is LOGIN WALLED for our crawl-heavy exit IP (`isOnboarded`
  false, `home/v2` `items:[]`, `/api/instamart/search/v2` 403 — traced 08-24),
  but `instamart.in` is a separate storefront that serves a default catalog
  WITHOUT that gate and resolves a localized darkstore once location is set.
  pw_catalog.js drives the app's own "Add your location" modal: it
  reverse-geocodes the anchor client-side (BigDataCloud, keyless — the
  `address-widgets/v2` response does NOT fire on instamart.in), types the
  locality, taps the first suggestion, then "Confirm Location". The granted
  browser GEOLOCATION then refines to the nearest store, so each anchor
  resolves its OWN `podid` + ETA (verified: Andheri→1404909/7min,
  Goregaon→1392421/6min). Default-catalog harvest still works if the modal is
  skipped. We do NOT log in (no fake accounts). Re-check / debug anytime:
  `DSH_DEBUG_DIR=/tmp/imdebug node tools/pw_catalog.js --app instamart
  --url https://instamart.in/ --lat <lat> --lon <lon>` then inspect
  /tmp/imdebug (products + store_hint in the last JSON line).
- WAF failover (08-30, #2 follow-on): the monitor loop leans on Blinkit when
  Zepto/Instamart get WAF-gated. `src/orchestrator.py` keeps a per-app strike
  ledger (`WafFailover`): N consecutive empty/errored crawls gates an app for
  `gate_cooldown_cycles` (it is then SKIPPED to avoid deepening the ban), and
  while any *other* app is gated, Blinkit gets `blinkit_priority_extra` extra
  per-station passes/cycle so coverage doesn't crater. Blinkit is in
  `protected_apps` (never auto-gated) — it's the resilient fallback because its
  APK has no AWS WAF and no request signature. Tunables live under
  `schedule.waf_failover` in config.yaml. Failover events emit `fkind="gate" |
  "skip" | "boost"` on the live event bus (`/status` shows them).
- Phase 4 (analysis) BUILT 08-22: `src/demand.py` + `--demand-report` +
  dashboard panels (DPI, onset heatmap, ETA curve). Phase 5 (hardening:
  proxy/IP coherence, per-store anchor rotation) still pending — see
  DEMAND_RADAR.md.
- Demand numbers are a stock-out-intensity PROXY for demand, never order
  volumes. Keep volumes modest; research only; no fake accounts/orders.
