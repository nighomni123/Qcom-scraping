# Agent Instructions (User Preferences)

## Web searches
- ALWAYS use `monid` (the Monid CLI) for web searches and web research.
- NEVER use the built-in `web_search` tool/plugin (or any DeepSeek/Exa-backed built-in search).
- Workflow: `monid discover --query "<what you need>"` to find a suitable data endpoint, then `monid inspect` and `monid run` to execute it.
- monid is installed at `/Users/Mitesh Gada/.npm-global/bin/monid`.
- monid writes its config/state via XDG paths, which the sandbox denies under `~/.config`. Always run it with `XDG_CONFIG_HOME="/Users/Mitesh Gada/Documents/Projects/.monid/xdg"` (workspace-writable; contains `monid/config.yaml` + `credentials.yaml`).
- Proven working call: `XDG_CONFIG_HOME=".../.monid/xdg" monid run --provider tinyfish --endpoint /search --query '{"query":"...","domain_type":"web"}' --wait 90 -j` (free endpoint; pass params via `--query`, not `-i`). Use `tinyfish /fetch` (free) with a JSON body `{"urls":[...]}` when full page text is needed.

## Ponytail — lazy senior dev mode (installed, applies to every project here)

You are a lazy senior developer. Lazy means efficient, not careless. The best code is the code never written.

Before writing any code, stop at the first rung that holds:

1. Does this need to be built at all? (YAGNI)
2. Does it already exist in this codebase? Reuse the helper, util, or pattern that's already here, don't re-write it.
3. Does the standard library already do this? Use it.
4. Does a native platform feature cover it? Use it.
5. Does an already-installed dependency solve it? Use it.
6. Can this be one line? Make it one line.
7. Only then: write the minimum code that works.

The ladder runs after you understand the problem, not instead of it: read the task and the code it touches, trace the real flow end to end, then climb.

Bug fix = root cause, not symptom: a report names a symptom. Grep every caller of the function you touch and fix the shared function once — one guard there is a smaller diff than one per caller, and patching only the path the ticket names leaves a sibling caller still broken.

Rules:

- No abstractions that weren't explicitly requested.
- No new dependency if it can be avoided.
- No boilerplate nobody asked for.
- Deletion over addition. Boring over clever. Fewest files possible.
- Shortest working diff wins, but only once you understand the problem. The smallest change in the wrong place isn't lazy, it's a second bug.
- Question complex requests: "Do you actually need X, or does Y cover it?"
- Pick the edge-case-correct option when two stdlib approaches are the same size, lazy means less code, not the flimsier algorithm.
- Mark deliberate simplifications that cut a real corner with a known ceiling (global lock, O(n²) scan, naive heuristic) with a `ponytail:` comment naming the ceiling and upgrade path.

Not lazy about: understanding the problem (read it fully and trace the real flow before picking a rung, a small diff you don't understand is just laziness dressed up as efficiency), input validation at trust boundaries, error handling that prevents data loss, security, accessibility, the calibration real hardware needs (the platform is never the spec ideal, a clock drifts, a sensor reads off), anything explicitly requested. Lazy code without its check is unfinished: non-trivial logic leaves ONE runnable check behind, the smallest thing that fails if the logic breaks (an assert-based demo/self-check or one small test file; no frameworks, no fixtures). Trivial one-liners need no test.

Canonical install: `.tools/ponytail` (update with `git pull`; source of the six `/ponytail*` skills — review, audit, debt, gain, help — and of this ruleset). When asked to review a diff or repo for over-engineering, follow `skills/ponytail-review/SKILL.md` and `skills/ponytail-audit/SKILL.md` there.

# AGENTS.md — qcom-scraping

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
   today's cheapest find per category + Demand Radar DPI top-5. Matching is
   token-overlap with an OPTIONAL semantic lift (`src/embed.py`: NVIDIA-hosted
   embeddings via ai.embedding_base_url, or the OPT-IN local Ollama
   embeddinggemma provider — ai.embedding_provider: "ollama"; cached in an
   additive `embeddings` table;
   blend = max(token, semantic) so it can only add candidates, never demote;
   endpoint down/disabled ⇒ exact historical token behaviour; backfill =
   `--embed-catalog`, archive query = `--similar`). A semantic catalog
   re-key guard was evaluated and REJECTED 09-05 — brand-sibling names
   cosine above genuine relabels, see src/watchlist.py.
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
- **Single implementation log**: all milestone/phase logs (M1, M2, …) live in
  ONE file — `IMPLEMENTATION_LOG.md` at repo root — NEVER per-phase files
  (`M1_*.md`, `M2_*.md`, etc.). Update it **proactively** as each step
  completes (do NOT wait to be reminded); append the next phase's status table
  + steps when its work begins. Keep the "Comments & deviations" as **one-line
  entries that state the reason for every deviation** from
  `PRODUCT_SPACE_PLAN.md`.
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
                        --search --bot --ui [--bot] [--monitor] [--no-monitor]
                        --once --map-locality --build-watchlist --demand
                        --store-inventory; --ui alone is dashboard-ONLY (no
                        loops); add --bot and/or --monitor to also run them
                        alongside; bare --bot keeps bot+monitor; flag
                        args parsed by _flag_* helpers)
config.yaml             every tunable; secrets go in .env only
codes.yaml              delivery fees + offer codes (user-editable)
GUIDE.md                combined first-run guide + plain-English glossary:
                        what to run in what order + how to interpret each
                        output, with every jargon term defined where it
                        first appears — link new contributors here before
                        the other docs
tools/pw_catalog.js     THE crawler: real app in headless Chromium, intercepts
                        signed API calls; deep-sweep visit queue (categories +
                        search terms; --skip filters category labels — the
                        demand.skip_categories milk de-biasing knob); --deep-cats
                        discovers sub-category links ON category pages (one-hop
                        BFS; catalog-inventory mode); --pre
                        pre-visits gated verticals before the target page
                        (Blinkit tobacco shelf, "URL::LABEL|..." pairs);
                        location seeding + request rewrite;
                        DSH_BODY_DIR raw-body dump hook
src/
  orchestrator.py       glitch-monitor loop (jitter, off-peak speedup)
  locality.py           phase 1: anchor grid + darkstore discovery/clustering
  watchlist.py          phase 2: per-store SKU probe set builder (+ catalog-
                        inventory mode: --catalog full snapshots + 'new'/
                        'delisted' churn diffs into catalog_snapshots/events)
  prober.py             phase 3: stock_obs loop + debounced oos_events machine
  demand.py             phase 4: DPI rollups, hour×SKU onset heatmap, ETA
                        curves, CSV export (pure functions over sqlite)
  categories.py         keyword product-category classifier (ordered rules)
  inventory.py          SINGLE-STORE full-category inventory capture
                        (--store-inventory): requires --app + --store, resolves
                        lat/lon from --lat/--lon (override — target ANY store,
                        even outside the machine's real location) or deals.db
                        .darkstores. Calls WatchlistBuilder.build_one_store()
                        (every-category deep sweep) and writes a RICH, COMPLETE
                        per-product record to inventory/inventory_<app>.db
                        (table inventory_catalog: name/price/mrp/in_stock/url/
                        collections [APP taxonomy]/category [internal taxonomy]/
                        raw_json [opaque app payload] + raw_json_truncated/
                        raw_json_bytes). The operational catalog_snapshots are
                        ALSO written to deals.db. Vouchers are captured here
                        and filtered downstream in the union layer.
  product_fields.py     grocery product-name FIELD PARSER (parse_name): brand,
                        pack_value/pack_unit, is_multipack, variant, unit_base,
                        unit_price, parse_status. Pure stdlib; self-test
                        python3 -m src.product_fields (20 curated cases).
  product_space.py      M1 UNION LAYER: load_product_space() reads inventory_
                        <app>.db.inventory_catalog UNION deals.db.catalog_
                        snapshots (latest per sku fallback), excludes vouchers,
                        attaches parsed fields, runs ENTITY-RESOLUTION product
                        groups (exact canonical key → within-brand match_score
                        candidate gen → pack/variant VETO → semantic confirm
                        fallback; semantics never override a pack/variant veto).
                        Provenance: every row carries app/store_id/sku_key/
                        inventory_db. Self-test python3 -m src.product_space.
  store.py              sqlite schema + all persistence helpers
  embed.py              OPTIONAL semantic matching (NVIDIA-hosted embeddings,
                        ai.embedding_base_url + NVIDIA_Build_API_KEY (asymmetric:
                        corpus=passage, query=query; nvidia/llama-nemotron-embed-vl-1b-v2 @2048
                        dims; a whole 1000-name batch counts as ONE request,
                        50 req/day free tier): batched stdlib-urllib client
                        + sqlite cache in an additive `embeddings` table
                        (one vector per distinct name, keyed model+dims so
                        a provider switch re-keys cleanly); search ranking +
                        WatchPusher blend max(token, semantic) so semantics
                        only LIFT candidates; endpoint down or
                        ai.semantic_matching: false => exact historical
                        token behaviour. Self-test: python3 -m src.embed
                        (offline, fake transport; fake vectors mirror live
                        cosines of the current model). Backfill attempts are
                        journaled to logs/embed_backfill.log (run start,
                        per-batch counts, stop reason + resume hint; the
                        embeddings table itself stays the source of truth
                        for which names are done). OPT-IN local provider
                        (ai.embedding_provider: "ollama"): embeddinggemma-300m
                        via the workspace-local Ollama server (.tools/ollama/),
                        no API key; its 768-dim rows sit BESIDE the NVIDIA
                        @2048 rows. Batch 256 is the measured optimum on this
                        2-core/8GB box (flat ~8 items/s for B=64→256; B≥512
                        exhausts free RAM and kills the Ollama runner — see
                        Environment quirks)
  ai_assist.py          OPTIONAL LLM advisor behind the dashboard AI panel
                        (/ai/*): result explanations, whitelisted demand-probe
                        tuning, product-focus staple_queries; OpenAI-compatible
  geo.py                corridor anchors + store resolver (glitch monitor)
  adapters/             blinkit / zepto / instamart / bigbasket /
                         jiomart / amazon / flipkart / trackers / demo;
                        base.py holds the browser machinery
  detect.py alert.py honey.py events.py dashboard.py search.py pricing.py
                        tgbot.py miniyaml.py (stdlib YAML fallback)
exports/                locality mapping JSON (rotation pools per store)
docs/                   research reports: QuickCommerce API vetting + Zepto/Swiggy
                        APK reverse-engineering findings (source .apkm files live in
                        apks/, gitignored — too large to commit)
scripts/live_sweep.py   one-shot real-glitch hunt
scripts/test_embeddinggemma.py  local-Ollama embeddinggemma backfill + demo
                        queries (self-managing: starts serve, pulls model)
scripts/compare_embeddings.py   embeddinggemma-vs-NVIDIA neighbour-agreement
                        comparison (numpy in .venv/ — run with .venv/bin/python)
scripts/embed_gemini_resume.py  additive GEMINI corpus backfill
                        (gemini-embedding-001 @768 via the AI_API_KEY pool,
                        config override in-process — repo default stays
                        NVIDIA; embedding_send_extras: false because Gemini
                        400s on the NVIDIA-only body fields; batch 100 +
                        16s pause ~= 94 RPM/key under the 100 RPM wall,
                        ~4k names/day across 4 keys, resets midnight PT;
                        re-run daily until pending hits 0)
scripts/bench_ollama_batch.py   Ollama batch-size throughput/RAM benchmark
scripts/embed_3d_map.py  Inventory Atlas: dark-themed semantic explorer over
                        the `embeddings` table -> exports/embedding_map.html
                        (self-contained; run with .venv/bin/python — needs
                        sklearn/plotly/scipy; --model nemotron|gemma renders
                        from either provider's vectors: nemotron (default,
                        2048d) -> embedding_map.html, gemma (768d) ->
                        embedding_map_gemma.html). Pipeline: cosine-normalize
                        -> PCA-50 -> k-means (--k, default 24) -> PCA-3 + PCA-2
                        (--projection umap upgrades both, needs umap-learn)
                        -> cheapest-latest price/store/category/stock join per
                        name -> one HTML (3D map + 2D hull-territory overview +
                        cluster explorer + click-to-select product inspector
                        with similar-products list; Semantic/Price/Store/
                        Category/Density modes restyle the same geometry).
                        --sample N renders N random names for fast UI testing;
                        --seed N fixes PCA/k-means/sampling; --neighbors N
                        sizes the inspector list (default 20)
.tools/ollama/          workspace-local Ollama binary (gitignored; models in
                        .ollama/, server HOME in .ollama_home/ — ~800MB total,
                        never commit)
deals.db                everything: price_obs, alerts, darkstores, watchlist,
                        stock_obs, oos_events, searches, search_results,
                        keyword_watches, catalog_snapshots, catalog_events,
                        embeddings (semantic cache, src/embed.py),
                        opportunities (M6/M7 scored-opportunity snapshots,
                        src/opportunities.py)
```

## Commands

    python3 run.py --check                 # config + adapters sanity
    python3 run.py --demo                  # offline pipeline test (injected glitch)
                                    # 09-04: runs against scratch deals.demo.db —
                                    # it can NEVER touch the real deals.db (it
                                    # used to wipe cfg.db before running; the
                                    # dashboard Demo card reset production once)
    python3 run.py --map-locality [--apps blinkit,zepto] [--max-points N]
    python3 run.py --build-watchlist [--apps …] [--store ID]
                                    [--max-per-store N] [--max-queries N]
                                    [--categories N] [--catalog]
                                    # --categories overrides categories_per_store
                                    # for one run; --catalog = catalog-inventory
                                    # sweep: ALL category links (--deep-cats one-
                                    # hop sub-category discovery), no search terms,
                                    # no skip_categories, writes catalog_snapshots
                                    # + diffs 'new'/'delisted' vs previous
                                    # (~20-60 min/store — run ONE store at a time)
                                    # streams per-visit progress live: store
                                    # (i/N) + label + queued-visit scope, then
                                    # one [sweep] line per visit with cumulative
                                    # SKU count — watch in the terminal or
                                    # Features ▸ Build watchlist ▸ log; mute
                                    # via anti_block.stream_progress: false
                                    # NOTE (09-02): the [sweep] "cumulative N"
                                    # is the live deduped products-Map size and
                                    # is the REAL per-store catalog count; the
                                    # crawler's stdout used to hard-cap at 400
                                    # (so a 5955-SKU sweep wrote only 400). Cap
                                    # is now --max-out (default 20000, never
                                    # truncates a real store). See
                                    # docs/catalog-sku-counting.md.
    python3 run.py --demand [--once] [--apps …] [--store ID] [--max-terms N]
    python3 run.py --demand-report [--store ID] [--csv]  # DPI table + heatmap summary
    python3 run.py --catalog-report [--store ID]         # catalog snapshots +
                                    # new/delisted churn log (catalog-inventory)
    python3 run.py --embed-catalog [--limit N]          # semantic backfill:
                                    # vectorize every distinct product name
                                    # via NVIDIA (ai.embedding_base_url +
                                    # NVIDIA_Build_API_KEY; asymmetric model
                                    # nvidia/llama-nemotron-embed-vl-1b-v2
                                    # @2048 dims; RESUMABLE — re-run to
                                    # continue, cached names are skipped).
                                    # 09-05 PROVIDER SWITCH from Gemini:
                                    # Gemini free /embeddings counted EACH
                                    # INPUT ITEM as its own request (100-name
                                    # batch = 100 RPM + 100 RPD; 1,000
                                    # RPD/key => ~9 days for 44k names).
                                    # OpenRouter counts a WHOLE BATCH as ONE
                                    # request: 1000 names/request (embedding_
                                    # batch), 44,487 names in 39 requests /
                                    # 14.5 min (live-measured). Free tier = 50
                                    # requests/day (X-RateLimit-Limit), $10
                                    # credits -> 1000/day. Model choice
                                    # live-verified on 60 real catalog names:
                                    # the only free model where BOTH
                                    # ground-truth pairs rank #1 ("diet coke"
                                    # ->Coke Zero +0.15, "cigarette"->Marlboro
                                    # +0.03). No `dimensions` support — fixed
                                    # 2048 dims (~8KB/name ~ 365MB for 44k;
                                    # probe smaller dims before DB growth
                                    # ever matters). Old Gemini rows in
                                    # `embeddings` stay (keyed model+dims);
                                    # the backfill re-keys on re-run.
                                    # 09-05 LOCAL OPTION (opt-in): set
                                    # ai.embedding_provider: "ollama" to embed
                                    # via workspace-local Ollama +
                                    # embeddinggemma-300m (768 dims, ZERO
                                    # quota). Harness: scripts/
                                    # test_embeddinggemma.py; quality
                                    # comparison vs NVIDIA: scripts/
                                    # compare_embeddings.py.
    python3 run.py --similar <phrase> [--limit N]       # semantic archive query:
                                    # names most similar to a phrase from the
                                    # embeddings table (needs --embed-catalog;
                                    # one live request for the phrase itself)
    python3 run.py --purge-vouchers [--dry-run]          # wipe voucher/gift-card
                                    # rows from watchlist/stock_obs/oos_events
                                    # (idempotent; --demand also auto-purges at
                                    # startup; backup kept as deals.db.bak-*)
    python3 run.py --qc-status [--apps blinkit,zepto,instamart]
    python3 run.py --store-inventory --app <app> --store <store_id>
                    [--lat X --lon Y] [--mirror-page-ms N] [--tabs N]
                                    # SINGLE-STORE full-category capture: one
                                    # store, every category (same engine as
                                    # --build-watchlist --catalog). Writes RICH
                                    # inventory_catalog rows to inventory/
                                    # inventory_<app>.db (name/price/mrp/in_stock/
                                    # url/collections/category/raw_json +
                                    # truncated/bytes flags) AND the operational
                                    # catalog_snapshots to deals.db. Location from
                                    # --lat/--lon (target ANY store, even outside
                                    # this machine's real location) or deals.db
                                    # .darkstores. Run ALONE — no concurrent
                                    # crawls (see rules).
    python3 run.py --product-fields [--report] [--sample N] [--db X]
                                    # grocery field-parser self-test (no flags)
                                    # OR --report: scans inventory DB(s) /
                                    # deals.db, attaches parsed fields, writes
                                    # exports/product_fields_sample.csv.
    python3 run.py --product-space [--csv]
                                    # M1 union layer: load_product_space() over
                                    # inventory_<app>.db.inventory_catalog UNION
                                    # deals.db.catalog_snapshots, voucher-excluded,
                                    # entity-resolved product groups; --csv ->
                                    # exports/product_space.csv.
    python3 run.py --product-vectors [--csv]
                                    # M2 attribute+commercial vectors per
                                    # product group (price/density normalized,
                                    # demand.dpi_table mean, store/app coverage,
                                    # churn flags); --csv -> exports/
    python3 run.py --detect-assortment-gaps [--csv]
                                    # M3 cross-app assortment gaps (present on
                                    # some but not all tracked apps)
    python3 run.py --product-density [--csv]
                                    # M4 per-category density + insufficient/
                                    # stale coverage guards
    python3 run.py --detect-gaps [--csv] [--min-n 25]
                                    # M5 internal/attribute gap scoring on top
                                    # of density guards
    python3 run.py --score-opportunities [--csv] [--persist] [--min-n 25]
                                    # M6 opportunity scoring
                                    # (gap_strength x DPI x coverage x churn)
                                    # with provenance + validation codes;
                                    # --persist writes the snapshot to the
                                    # deals.db `opportunities` table (M7)
    python3 run.py --opportunity-report [--limit N]
                                    # latest persisted opportunity snapshot

Dashboard (`--ui`, http://127.0.0.1:8787; dashboard-ONLY by default — starts
no loops; add --bot and/or --monitor to also run them alongside, or start
loops from the Features panel) endpoints: `/status /categories
/searches /search/<id>` (bot analytics), `/demand /heatmap?store=
/eta /qc` (Demand Radar, DB-backed — live probing stays in `--qc-status`),
`/db` + `/db/<db>/<table>` (read-only browser over ALL repo sqlite files:
deals.db + inventory/*.db), `/semsearch?q=<text>` (offline lexical product-name
lookup) + `/semsearch?seed=<name>` (offline stored-vector expansion across the
gemma/google/nematron silos — the Semantic search tab's 3 panels; no
embedding-model call), `/features` + `/features/<id>/start|stop|log`
(spawn/terminate any repo feature — monitor loop, bot, --demand, demo,
inventory sweeps… — as managed child processes; 09-02: features declare an
editable-args spec (`args` in FEATURE_CATALOG), the panel renders inputs per
feature and POSTs {"args": {"--store": "34292", "--catalog": true}}; the
server whitelists EVERY flag against that spec — unknown flags are rejected
(400), never forwarded, so the panel can never become an arbitrary-CLI
runner — and an override REPLACES the base cmd's occurrence of the same flag
(run.py reads the FIRST occurrence, appending a duplicate would be ignored).
Last-used values persist in the browser's localStorage) and `/location`
+ `/location/presets` + POST `/location/locality` | `/location/corridor`
(09-02: GET /location also carries `darkstores` — every recorded darkstore
in deals.db pinned on the working-area map with app-coded ▲◆■ icons, a
per-app count legend and a popup (app/store-id/label/ETA); pins are
reference-only and never move the working pin)
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
- **Blinkit category-link shape for catalog sweeps** (probed 09-02 via DOM href dump):
  the home page's category rails and the `/categories` hub expose shelves as
  `/dc/<l0-slug>/<l1-slug>/?collection_uuid=<b64>&collection_group_id=<gid>`
  collection routes (308 links on the hub, 29 on home) — NOT `/cn/` (that shape
  carried exactly ONE link: E-Gift Cards, 100% vouchers). The old
  `collectCatLinks` regex `/(cn|category|c)\//` matched neither, so Blinkit
  catalog sweeps silently collapsed to the home feed (~104 SKUs, all
  `collections=home`). Fixed: regex is now `/(cn|category|categories|c|dc|sc)(\/|$)/`
  — matches `/dc/` routes and the bare `/categories` hub. `/dc/` pages DO yield
  products in warm sessions (`listing_widgets` is_success:true, ~300 products
  per shelf); verified 12-visit sweep → 540 real SKUs across 10 categories
  (zero vouchers) vs the broken behavior's 104 all-home.
- **Instamart subcategories + inner-container pagination** (probed 09-02 via the
  user's cold-drinks example): category pages expose their real shelves as
  `/sc/<l0>/<l1>-<id>/t10` subcategory routes (19 on the cold-drinks page —
  e.g. `/sc/cold-drinks-and-juices/soft-drinks-6903b0c295d8230001064c75/t10`),
  which the category regex now also matches (`sc` added). TWO pagination layers:
  the product grid lives in an INNER scrollable container (not the window), and
  the next page fires ONLY when that container is scrolled to its BOTTOM —
  incremental window scrolling never advances pagination (old behaviour: ~20
  products per category while the page advertises "1488 items"). The listing
  call is POST `/api/instamart/category-listing/filter/v2` whose BODY carries
  the real cursor `items_offset` (26 → 46 → 66 → …, stride 20 — the URL's
  `pageNo`/`offset` params are decoys: replaying with only those params
  returns the same page 1 products). The crawler therefore (a) logs the
  page's advertised "N items" per visit as a honesty signal, (b) MIRRORS the
  visit's own captured listing POST with advancing `items_offset` (the
  established apiHits mirror pattern — session's own auth, no forging), and
  (c) falls back to bottom-jump deep scroll only when a visit fires no
  listing call — OPT-IN via `--scroll-rounds` (default 0 = off; Blinkit /dc/
  and Zepto baselines were captured at page-1 shelf depth, and a depth change
  vs a shallower baseline emits one-time 'new' events — keep depth consistent
  per store for honest churn). The 2.2s bait jump that triggers the listing
  POST is gated to `instamart` only (extend when a new app gains a paginated
  listing call) — Blinkit/Zepto pay nothing extra and hold their baseline
  depth. Tunables: `--mirror-stride 20 --mirror-max-pages 80 --mirror-page-ms
  900 --scroll-rounds 0` (mirror pacing ~1s/page mirrors organic scroll;
  scroll rounds ~2-4s each when enabled). `--mirror-page-ms` is now an
   overridable runtime flag (lowering it below 900 speeds a catalog run but
   raises Instamart's throttle/ban risk — 600 is the floor we test at).
 - **Multi-tab catalog sweeps (`--tabs N`, default 1):** when a deep-cats
   catalog run feels too slow, pass `--tabs 2` (or 3) to open N concurrent
   Playwright **tabs inside the SAME browser context** — same store, same
   `device_id`, one session per store (the invariant at the bottom still
   holds; this is intra-session concurrency, not a second session). The tabs
   share one visit queue, so deep-cat link discovery feeds every tab, and each
   tab keeps its OWN intercepted-api list + collection label so products are
   tagged with the right shelf. Use 2-3 to cut wall-clock on Instamart
   (full catalog ~40 min single-tab → ~15-20 min at 3 tabs). Cross-tab
   throttling is still possible — the user tests `--tabs 3 --mirror-page-ms
   600` on Instamart to measure it. Same-app parallelism is still BANNED
   (don't run two stores of one app at once); tabs are within ONE store.
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
- **Local embeddings via workspace Ollama** (added 09-05, OPT-IN): the Ollama
  server binary lives INSIDE the repo workspace at `.tools/ollama/ollama`
  (Intel-Mac build v0.33.3 from GitHub releases — no brew on this box), models
  in `.ollama/` (~621MB `embeddinggemma` = google/embeddinggemma-300m, gemma3
  family, 768-dim native, matryoshka 256), server HOME in `.ollama_home/`.
  SANDBOX QUIRK: Ollama writes its id_ed25519 key under `$HOME/.ollama`, which
  the sandbox denies — `HOME`/`OLLAMA_HOME` MUST point into the workspace or
  `ollama serve` dies instantly with "could not create directory" (`OLLAMA_HOME`
  alone is NOT honored for the key dir; set `HOME`). Batch-size reality
  (benchmarked 09-05 via scripts/bench_ollama_batch.py on this 2-physical-core
  / 8GB box): throughput is FLAT ~8 items/s from B=64→256 — CPU-bound, request
  overhead is NOT the bottleneck locally, so single-item embeds are never
  better — and free RAM bleeds as the runner holds its KV-cache high-water
  mark; **B≥512 exhausts free RAM and the runner process dies**, surfacing as
  HTTP 400 `Post "http://127.0.0.1:<port>/tokenize": connection reset by peer`
  (NOT a validation error — the server still answers /api/tags but rejects
  every embed until restarted). Hence `embedding_batch: 256` is the optimum;
  `.tools/`, `.ollama/`, `.ollama_home/` are all gitignored.
   **Global launcher**: `.tools/ollama/ollama-global` wraps the binary and
   pins `HOME`/`OLLAMA_HOME`/`OLLAMA_MODELS` into the workspace, then `exec`s
   it; it is symlinked as `/usr/local/bin/ollama` so `ollama` works from
   anywhere as a normal global command WITHOUT per-run sandbox escalation
   (all writes stay inside the repo). Use that, not the bare binary, so the
   id_ed25519 key + models are reused from `.ollama_home`/`.ollama`.

## Demand Radar invariants (do not break)

- **Vouchers are not commodities (09-02).** Digital vouchers / gift cards
  (Steam/Roblox/Xbox/retail "Instant Voucher" cards, Blinkit Gift Card, …)
  are excluded from EVERY demand surface: the watchlist builder drops them,
  the prober skips them before observations/events/soft-block guards, and
  DPI rollups ignore them defensively. The line is drawn by
  `src/store.py → is_voucher_name()` (name tokens "voucher"/"gift card"
  ONLY — never add brand substrings like "steam", they false-positive on
  garment steamers). Knob: `demand.exclude_vouchers` (default true).
  Cleanup: `run.py --purge-vouchers` (idempotent; `--demand` auto-purges at
  startup). Their "stock-outs" are code-pool replenishments, not demand.
- **Delisting is snapshot-driven only (09-02).** `catalog_snapshots` +
  `catalog_events` ('new'/'delisted') come ONLY from full-catalog sweeps
  (`--build-watchlist --catalog`): every category link, NO search terms, NO
  skip_categories (a skipped shelf would fabricate delistings), one-hop
  sub-category discovery via pw_catalog.js --deep-cats. Absence from a
  partial sweep or from the prober's light rounds is NEVER churn. A snapshot
  whose distinct (name,price) pair count collapses to <70% of the previous
  one is recorded but its diff is SKIPPED (mass absence = crawl flake, not
  churn — suspect-cycle freeze analog; 09-04: raised 0.5→0.7 and keyed to
  pairs after a 56%-pair-count Instamart sweep fabricated ~11.5k delistings).
  Key-rotation noise (09-04): sku_key falls back to a name-slug on shelves
  whose payload omits every id field, and products re-key slug→id between
  sweeps — the diff RECONCILES on exact (name,price) pairs: a "new"/
  "delisted" key whose pair exists on the other side is a re-key, NOT churn
  (excluded from events + watchlist deactivation; run log prints
  "rekeyed (not churn): N"). Self-test: `python3 -m src.watchlist`.
  Delisted SKUs: watchlist.active=0, rows KEPT (the watchlist
  IS the discontinued archive); no vanished oos_event from this path — the
  prober's vanished machine keeps its own stock-out semantics. Dashboard:
  GET /catalog?store=; report: --catalog-report.
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
  09-04 guard: vanished opens ONLY for SKUs with prior stock_obs sightings —
  the exhaustive catalog made the watchlist a full inventory while a light
  sweep sights only a few hundred SKUs/cycle, so an unguarded restart would
  have fabricated thousands of vanished events per store. A catalog-built
  never-sighted row is a coverage gap, never churn; catalog-covered stores
  get delisting verdicts from the snapshot diff (catalog_events), not the
  prober.
- **Suspect cycles freeze the machine.** Canary queries returning only-OOS or
  a mass in-stock→OOS flip ⇒ record observations, open/close nothing.
- **Onset windows need silent-hour coverage.** `demand.sweep_windows`
  (local hours; `"0-23"` = all) gates when the `--demand` loop STARTS rounds.
  The Aug 24–Sep 1 history had ZERO observations in hours 02–10 and 15–16, so
  OOS onsets in those windows were only recorded late (event `started_at` = the
  first read after the gap) — the onset heatmap is biased to sweep hours. Keep
  the prober running 24/7 or set `sweep_windows` to the silent windows to fill
  them; the loop idles outside windows and never stalls a round in flight.
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

## Alert invariants (glitch monitor, 09-02)

- Every price alert (desktop / Telegram / `deals.log` / live feed + modal)
  carries a reference "was" price: the catalog **MRP** when the crawl payload
  has one, else the **usual price** — `Store.usual_price()` = median of that
  (store,sku)'s recent `price_obs` EXCLUDING the newest row (the triggering
  observation must not define its own "usual"). `alerts.mrp` persists it
  (additive column, backfilled on old DBs); the `alert` event carries
  `price`/`mrp`/`usual` fields for the dashboard. WatchPusher /watch pushes are
  a different surface and do not include it.

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
  pw_catalog.js drives the app's own "Add your location" modal: type the
  locality term, tap the first suggestion, then "Confirm Location" (the
  granted browser GEOLOCATION then refines to the nearest store). The TERM is
  resolved ONCE per run and shared by the homepage warm-up binding and the
  target-page localize (09-04 fix; before, warm-up reverse-geocoded while
  localize used a separate "rotated" term, and since each probe spawns a FRESH
  node process the process-local rotation counter was always 0 — so every
  anchor typed the same word, warm-up's first suggestion bound ONE store, and
  the localize re-bind failed silently; all corridor anchors collapsed onto a
  single downtown darkstore). Term priority: `--im-term` (caller-supplied) →
  `address-widgets` capture → BigDataCloud client reverse-geocode (keyless;
  `address-widgets/v2` does NOT fire on instamart.in) → deterministic
  corridor fallback (coordinate-hash over local suburb names — a process-local
  counter can never rotate across fresh processes). `src/locality.py` supplies
  `--im-term` per anchor: landmark anchors pass their config name ("Andheri"),
  grid anchors rotate through the config landmark names; other apps ignore the
  flag. Default-catalog harvest still works if the modal is skipped; a failed
  localize now logs "[localize] ... modal did not re-open" instead of nothing.
  Verified bindings (09-04 smoke, 4 distinct stores across 6 anchors):
  Andheri→1295147/14min, Goregaon→1404958/9min, Borivali→1392421/4min,
  grid r6c4→1403051/10min. We do NOT log in (no fake accounts). Re-check /
  debug anytime:
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
  dashboard panels (DPI, onset heatmap, ETA curve). Phase 5 (hardening)
  IN PROGRESS 08-31: the proxy plumbing is now WIRED end-to-end —
  `anti_block.proxies` (config) + `PROXY_URL` env (comma pool) →
  `pick_proxy()` per invocation → `--proxy` → `browser.newContext({proxy})`
  in pw_catalog.js (fresh browser+context per run == fresh WAF identity, per
  the session-bound-token constraint; parseProxy rejects junk hosts).
  Empty pool = exact old behaviour (verified: blinkit probe unchanged).
  STILL PENDING: procure a Mumbai-exit residential pool (user decision/cost),
  validate IP↔anchor coherence live (jiomart store MUST change per exit IP —
  it is IP-locked; that's the acceptance test), then stagger/jitter + global
  req/hr cap + backoff per DEMAND_RADAR.md Phase 5.
- Expansion apps (08-31): `bigbasket` / `jiomart` adapters are WIRED (search,
  QC_APPS, orchestrator, pricing, dashboard) but stay `enabled: false` — the
  blockers are ENVIRONMENTAL, not code (an earlier "needs location seeding"
  guess was wrong; tracing proved otherwise):
  * bigbasket: Akamai Bot Manager 403s our headless Chromium on EVERY route +
    UA (curl passes, browsers don't; `errors.edgesuite.net`). No bbnow/alt
    domain resolves. Revisit only with a residential proxy or real-Chrome
    fingerprint.
  * jiomart: extraction WORKS now (pw_catalog.js harvests 12+ products with
    price `effective.min`, MRP `marked.min`, `in_stock_variant`, `store_ids[]`,
    ETA `eta_mins` — all added 08-31). BUT the QC store is resolved SERVER-SIDE
    from the request IP (delivery-promise returned the same store at an
    identical 820.3851 m across runs whose app_geolocation cookie held 3
    different values, and pre-cookie too) → NOT anchor-steerable, so it
    violates "location is enforced, not assumed" and can't feed Demand Radar
    per-anchor mapping; at best a machine-location --store-inventory source,
    and its search endpoint rate-limits hard (~4 rapid probes → empty, ~2.5 min
    recovery). Do NOT add client-side location seeding for jiomart — it does
    nothing (see the NOTE in pw_catalog.js's init script).
  DROPPED the same day with evidence: `dmart` (web login-gates; no fake
  accounts) and `amazon_now` (Amazon Now IS live in Mumbai — expanded
  Sept–Nov 2025 after Bengaluru/Delhi, 100+ dark stores, but only in SELECT
  neighbourhoods and with NO standalone app: it's a pincoded delivery option
  inside the main Amazon app. The web routes serve generic search or the
  empty legacy Fresh shell; would pollute price_obs). Full
  detail: README "Adapter expansion".
- Demand numbers are a stock-out-intensity PROXY for demand, never order
  volumes. Keep volumes modest; research only; no fake accounts/orders.
