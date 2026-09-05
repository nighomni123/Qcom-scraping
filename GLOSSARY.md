# GLOSSARY.md — plain-English index of every jargon term in this repo

For someone opening the dashboard, a log, or `config.yaml` for the first
time and thinking *"what does OOS have to do with anything?"*. Terms are
grouped by where you'll meet them. If a definition uses another term, that
term is defined here too (or is common English).

## The five you need immediately

- **QC (quick commerce)** — the apps that deliver groceries in ~10 minutes:
  Blinkit, Zepto, Swiggy Instamart. This tool watches all three.
- **SKU** ("skew") — one specific sellable product, e.g. "Amul Gold milk 1L"
  is one SKU; "Amul Gold 500ml" is a different one. Short for Stock Keeping
  Unit.
- **OOS (out of stock)** — the app shows the product but says "currently
  unavailable". The Demand Radar's whole job is detecting when and where
  this happens.
- **Darkstore** — the small warehouse an app delivers from in your
  neighbourhood. You never see it; each one has its own stock and its own
  delivery time. In this repo a store is identified by a `store_id`
  (e.g. `blinkit::34654`).
- **Glitch** — a pricing mistake: the app shows a price far from the
  product's normal one (₹1 milk, negative discount…). The "deals finder" /
  "glitch monitor" exists to catch these and alert you.

## Shopping-app vocabulary

- **Home carousel / category rail** — the scrolling product rows and the
  category strip on the app's front page. Both are ordered
  **dairy-first** (milk, bread, eggs) because that's what people buy most —
  which is why raw crawling over-samples milk, and why the config has
  `skip_categories` and `crawl_terms` de-biasing knobs.
- **MRP** — Maximum Retail Price, the printed ceiling price. A price far
  below MRP is either a real sale or a glitch.
- **ETA** — Estimated Time to Delivery, the "8 min" badge. It's per
  darkstore, never per product.
- **Delisting / vanished** — a product disappears from the app entirely
  (not just OOS). We record it as a `vanished` event — deliberately NOT
  counted as out-of-stock, because absence of evidence isn't evidence.
- **Honey pot / canary** — a handful of products whose TRUE price (honey
  pot) or true in-stock status (canary — e.g. milk, which is essentially
  never out of stock) we know. If those look wrong, something is wrong with
  OUR crawler, not the store. Named after canaries carried into coal mines.
- **Baseline** — the normal price we learn for each SKU at each store from
  history. Deviations from it are what trigger glitch alerts.

## Demand Radar (the stock-out intelligence feature)

- **Watchlist** — per-store memory of SKUs we care about, so a product
  going missing is a signal instead of blindness. Built by
  `--build-watchlist`.
- **Probe / sweep** — one crawl pass over a store. A **deep sweep** is the
  full pass: home page → up to N category pages → search terms, all in ONE
  browser session.
- **Prober** — the loop (`--demand`) that re-sweeps stores on a timer and
  writes stock observations.
- **stock_obs** — the table of every observation: (store, SKU, in-stock
  yes/no/unknown, price, time). The raw evidence.
- **oos_events** — confirmed stock-out incidents: an event opens only after
  `oos_debounce_snapshots` (config) consecutive zero-reads, and closes the
  moment the item is seen in stock again.
- **Debounce** — ignoring single flaky readings; requiring N consecutive
  matching observations before believing anything. Prevents one bad page
  load from fabricating a stock-out.
- **Streak** — how many consecutive sweeps an SKU has been read out-of-
  stock. **Restart-proof** means the counter is rebuilt from history, so
  restarting the prober doesn't reset it or fake a start time.
- **NULL ≠ OOS** — the golden rule: if a crawl failed or a field didn't
  parse, we record "unknown" (NULL), never "out of stock". Broken scrapers
  must not invent demand.
- **Suspect flip** — if a whole store's items flip in-stock→OOS at once,
  that's almost certainly us being soft-blocked, not the store emptying.
  The machine freezes event changes for that cycle.
- **DPI (Demand Pressure Index)** — this repo's stock-out intensity score
  per SKU: roughly (total out-of-stock minutes) × (recency) ÷ (days we
  actually observed it). High DPI = keeps going out of stock, recently.
  It's a PROXY for demand — we see shelves, never order volumes.
- **Onset heatmap** — hour-of-day × SKU grid showing WHEN stock-outs start
  (e.g. "eggs vanish by 7 PM"). A blank cell means the prober never ran
  then — not that everything was fine.
- **Locality / corridor / anchor / grid** — the geography layer: a
  **locality** (e.g. Andheri West) is cut into a **grid** of GPS **anchors**;
  each anchor, fed to the app, resolves to the nearest darkstore. The
  **corridor** is the line of stations (Virar→Andheri) the glitch monitor
  crawls along.
- **Catchment** — the area one darkstore actually serves.
- **Rotation pool** — the set of anchors that resolve to the same
  darkstore; saved under `exports/` so mapping doesn't redo all the work.
- **Saturation stop** — during mapping, stop probing once new probes stop
  finding new stores.
- **Phases 1–5** — the Radar's build order: 1 map stores → 2 build
  watchlists → 3 probe stock → 4 analyze (DPI/heatmap/ETA) → 5 hardening
  (proxies, anchor rotation). Status lives in `DEMAND_RADAR.md`.

## Glitch monitor / deals finder

- **Cycle** — one full pass over every station × app. `cycle_seconds` sets
  the pace; **jitter** = random ±30% so we don't look like a metronome.
- **Off-peak speedup / quiet hours** — crawl FASTER late night (23:30–
  07:30) because the apps' rate limiters are laxer then.
- **Deviation %** — how far a price sits from baseline/honey true price;
  crossing `detect.*` thresholds raises an alert.
- **Z-score** — statistically how weird a price is relative to that SKU's
  own history (in units of "standard deviations"); catches glitches on
  items whose normal price varies.
- **WAF** — Web Application Firewall (AWS WAF guards Zepto/Instamart): a
  bouncer that blocks traffic it decides is robotic. Getting "gated" =
  temporarily blocked.
- **Strike / gate / cooldown / boost (WAF failover)** — the monitor keeps
  score: N consecutive empty crawls **gates** an app (stops touching it so
  the ban cools down) for a few **cooldown** cycles, and gives Blinkit
  (which has no WAF) extra passes as a **boost** so coverage doesn't
  crater.
- **Exit IP** — the internet address your traffic appears to come from.
  Heavy crawling can get that address reputation-blocked; proxies rotate
  them (Phase 5 territory).

## Telegram bot

- **/watch `<product>`** — "ping me when this shows up cheap in any crawl or
  search". Stored in `keyword_watches`; matched by **token overlap**
  (shared words) scoring.
- **/digest** — daily summary: cheapest find per category + DPI top-5.
- **Rate cap / cooldown** — hard limits on pushes per hour and per watch,
  so the bot can't spam you.

## Dashboard (http://127.0.0.1:8787)

- **Features panel** — start/stop/log for each long-running job (monitor,
  prober, bot…). Each feature runs as a **child process** of the dashboard;
  stopping the dashboard stops them all.
- **Live feed** — recent crawl events with sample products, per batch.
- **Endpoints** — the dashboard's data URLs (`/status`, `/demand`,
  `/heatmap`, `/eta`, `/categories`, `/searches`, `/db`, `/ai/*`…) that
  return **JSON** (structured text the page renders).
- **DB browser** — read-only view of every sqlite file in the repo.
- **Log tail** — the last N lines of a feature's output.

## Semantic matching (embeddings)

- **Embedding** — a list of numbers (a **vector**) that a model assigns to a
  piece of text so that texts with similar MEANING get similar vectors, even
  with zero shared words ("diet coke" vs "Coca-Cola Zero Sugar 750ml").
- **Cosine similarity** — the standard 0–1 measure of "how close" two vectors
  are (angle between them). In this repo, live Gemini cosines: unrelated
  products ~0.50, category matches ~0.58–0.66, same product ~0.65–0.80.
- **`embeddings` table** — the cache in deals.db: one vector per distinct
  product name (additive; nothing else reads it, safe to ignore or wipe).
- **Semantic lift** — search/watch scores become `max(token_score,
  semantic_score)`: meaning can only ADD a candidate the word-match missed,
  never demote one that matched. Endpoint down = the old word-only behaviour.
- **Backfill** (`--embed-catalog`) — vectorize every distinct product name
  once (OpenRouter counts a whole 1000-name batch as ONE request: ~45
  requests for 45k names, ~15 min); resumable, re-run anytime.
- **`--similar <phrase>`** — archive query: which known product names are
  semantically closest to a phrase you type.

## AI panel ("Understand the results")

- **LLM** — Large Language Model; the chat AI answering in the panel.
- **Digest** — the compact stats pack we hand the model (DPI table,
  heatmap, ETA, counts). The model only knows what's in here + your
  question — it does not crawl.
- **Tokens / max_tokens** — an LLM's billing/length unit (~¾ of a word);
  `max_tokens` caps the answer. Long analyses are sidestepped by saving the
  full report as a downloadable markdown file instead.
- **OpenAI-compatible / base_url / model** — the config `ai:` block: any
  provider speaking the same HTTP dialect works (Gemini does; local Ollama
  runs keyless). `AI_API_KEY` in `.env` is the secret.
- **429 / rate limit** — "too many requests" from the free AI tier; wait a
  few seconds between analyses.
- **Follow-up chat** — questions about the last analysis; each Q&A is
  appended to the saved report file.

## Crawler internals (how we see anything at all)

- **Headless Chromium / Playwright** — a real Chrome browser with no
  window, driven by code (`tools/pw_catalog.js`). The apps can't tell it
  from a phone browser.
- **Browser fingerprint / mobile WebView UA** — the settings (screen size,
  user-agent string) that make the headless browser masquerade as the
  apps' own in-app browser. **UA** = User-Agent, the "who am I" header every
  browser sends.
- **Intercept / mirror** — instead of guessing the apps' private API, we
  let the real page make its own calls and copy the answers out of the
  network traffic.
- **Signed requests / signature** — some apps cryptographically stamp
  every API call (Zepto: `request-signature`, HMAC). We never forge them —
  the browser produces them naturally, we just harvest the responses.
- **Location seeding / request rewrite** — the apps cache "which city am I
  in" aggressively (Blinkit once served us Gurugram from Mumbai). We force
  the target location via localStorage + cookies + rewriting lat/lon inside
  outgoing requests.
- **Onboarding gate / CTA** — popups like "Add your location"; **CTA** =
  call-to-action button. The crawler clicks them itself when a fresh
  session gets stuck.
- **DOM** — the live HTML structure of the loaded page; where category
  links are scraped from.
- **Cert pinning / mitm** — apps embedding their TLS certificate to defeat
  traffic sniffing. None of the three do (mitm = man-in-the-middle capture
  is a viable fallback).
- **Field-name discovery (`DSH_BODY_DIR`)** — apps rename their JSON fields
  regularly; the dump hook saves raw responses so we re-learn names instead
  of guessing.

## Data & code plumbing

- **sqlite / `deals.db`** — the single file-based database holding
  everything (price_obs, stock_obs, alerts, watchlist, …).
  `inventory_<app>.db` are separate per-app snapshots from
  `--store-inventory`.
- **Table / row / column** — a database spreadsheet: rows are records,
  columns are fields.
- **Schema** — the set of tables and columns. **Additive only** is a hard
  repo rule: never drop/rename, only add — other features read it live.
- **Backfill** — safely adding a new column to an existing table by
  creating it empty and filling old rows.
- **ts** — timestamps everywhere are unix epoch seconds (floats), not
  human dates.
- **miniyaml** — the repo's tiny built-in YAML reader used when PyYAML
  isn't installed. It only understands a subset — which is why
  `config.yaml` must stay "miniyaml-compatible" (simple `key: value` and
  `[a, b]` / `["a", "b"]` **flow lists**, no fancy YAML features).
- **config.yaml / codes.yaml / .env** — tunables (safe to share) / delivery
  fees + offer codes / secrets. Secrets NEVER go in the first two, and
  `.env` is **gitignored** (never committed).
- **exports/** — generated artifacts: locality maps, AI report markdown
  files (gitignored).
- **`run.py --check`** — the sanity gate: config parses with both YAML
  loaders, adapters import, counts look sane. The "test suite" is
  `py_compile` (Python syntax check) + `node --check` (JS syntax check) +
  `--check` + module self-tests (`python3 -m src.prober`).
- **CLI flags** — `--demand` (run the prober loop), `--demand --once` (one
  sweep), `--build-watchlist`, `--map-locality`, `--store-inventory`,
  `--search "term"`, `--bot`, `--ui`, `--demo` (offline simulation with an
  injected fake glitch), `--qc-status` (one-shot store liveness check).

## Repo map shorthand

- **`src/`** — the Python package; one module per feature (see AGENTS.md
  repo map). **`tools/`** — the JS crawler. **`docs/`** — reverse-
  engineering reports on the apps' APIs/APKs. **`scripts/`** — one-off
  tools. **`apks/`** — raw installer files for reverse-engineering
  (gitignored, too big).
