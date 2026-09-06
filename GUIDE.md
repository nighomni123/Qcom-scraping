# GUIDE.md — first-run guide & plain-English glossary

What to run, in what order, and how to read the results the first time you
use this tool — with every jargon term defined in plain English **where it
first appears**. ~30 minutes to a working setup, then it runs itself.
(This file merges the former `GETTING_STARTED.md` and `GLOSSARY.md`.)

Companions: `README.md` (reference), `DEMAND_RADAR.md` / `ARCHITECTURE.md`
(design), `AGENTS.md` (contributor rules).

## Vocabulary first — the five you need immediately

For someone opening the dashboard, a log, or `config.yaml` for the first
time and thinking *"what does OOS have to do with anything?"*. Every other
term is defined later, in the stage where you meet it.

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

## The four golden rules

1. **One long crawl at a time.** `--map-locality`, `--build-watchlist`,
   `--demand`, `--store-inventory` all hit the same apps; running two gets
   both rate-limited into empty results. The dashboard **Features panel**
   shows what's running — check before starting another.
2. **The dashboard is the control room.** Everything long-running is best
   started/stopped from **Features** (they run as managed **child
   processes** of the dashboard; STOP ALL reaps them). Terminal runs work
   too, but you own the process.
3. **Config edits apply on next start.** Editing `config.yaml` does nothing
   to a running feature until you stop/start it.
4. **Absence of evidence ≠ evidence.** Unknown stock is recorded as NULL
   ("unknown"), never "out of stock"; a product that vanished from the app
   is a `vanished` event (delisted/hidden), not `oos` (listed but
   unavailable — different stories, don't merge them); a blank heatmap cell
   means *we weren't watching*, not *all was fine*. Broken scrapers must
   not invent demand.

## Stage 0 — Install & sanity (5 min, offline)

    cd <repo>
    python3 run.py --check     # config parses (both YAML loaders), adapters import
    python3 run.py --demo      # full pipeline against fake data

`--check` must print `config ok; stations: N`. It is the sanity gate: config
parses with both YAML loaders, adapters import, counts look sane. (The
repo's whole "test suite" is `py_compile` (Python syntax check) + `node
--check` (JS syntax check) + `--check` + module self-tests like `python3 -m
src.prober`.)

`--demo` runs the whole detect→alert pipeline **offline** with an *injected
fake glitch* — if you see an alert for it, your detection engine works.
Nothing in demo mode is real; never interpret demo output as market data.

The three config files, and what belongs where:

- `config.yaml` — every tunable (safe to share).
- `codes.yaml` — delivery fees + offer codes (user-editable).
- `.env` — secrets (e.g. `TG_BOT_TOKEN`/`TG_CHAT_ID` for Telegram push,
  `AI_API_KEY` for the AI panel; copy `.env.example`). **gitignored** —
  never committed. Secrets NEVER go in the first two files.

`config.yaml` must stay **miniyaml-compatible**: the repo's tiny built-in
YAML reader (used when PyYAML isn't installed) only understands a subset —
simple `key: value` and `[a, b]` / `["a", "b"]` **flow lists**, no fancy
YAML features.

Optional pieces: the AI panel needs `AI_API_KEY` + the `ai:` block in
config. The browser layer needs no install (Playwright + Chromium are
pre-installed elsewhere and wired via `anti_block.*` in config).

## Stage 1 — Open the dashboard (2 min)

    python3 run.py --ui            # http://127.0.0.1:8787 — dashboard ONLY,
                                    # starts no loops; add --bot and/or --monitor
                                    # to also run them, or start loops from
                                    # Features below

Tour, left to right: **Status** (the **live feed** — recent crawl events
with sample products per batch — plus alerts), **Categories** (what your
crawls actually cover), **Demand Radar** (DPI table, onset heatmap, ETA
curves), **Searches** (bot history), **SQL databases** (the **DB browser**
— a read-only view of every sqlite file in the repo: `deals.db` +
`inventory_*.db`), **Features** (start/stop/**log tail** — the last N lines
of a feature's output — for every job), **Location** editor, **AI** panel.

The panels are filled by **endpoints** — the dashboard's data URLs
(`/status`, `/demand`, `/heatmap`, `/eta`, `/categories`, `/searches`,
`/db`, `/ai/*`…) that return **JSON** (structured text the page renders).

Interpretation: the dashboard only *reads* the database — if a panel is
empty, the feature that fills it isn't running yet. Start with Features.

## Stage 2 — The deals finder (glitch monitor)

Fastest real-data win. Either: dashboard **Features ▸ Glitch monitor ▸
Start**, or one cycle from the terminal:

    python3 run.py --once          # single corridor pass, then exits

A **cycle** is one full pass over every station × app (home page + honey-pot
searches + one rotating `schedule.crawl_terms` query); `cycle_seconds` sets
the pace, with **jitter** (random ±30%) so we don't look like a metronome.
An **off-peak speedup** crawls FASTER during **quiet hours** (23:30–07:30)
because the apps' rate limiters are laxer then. The **corridor** is the
line of stations (Virar→Andheri) the glitch monitor crawls along.

What to watch:

- **Live feed**: `blinkit @ Andheri — 214 products, 8.2s` = healthy.
  `products=0` on an app that usually returns hundreds = **WAF** gate or
  soft-block. A **WAF** (Web Application Firewall — AWS WAF guards
  Zepto/Instamart) is a bouncer that blocks traffic it decides is robotic;
  getting **"gated"** = temporarily blocked. The failover ledger keeps
  score (**strike / gate / cooldown / boost**): N consecutive empty crawls
  *gates* an app (stops touching it so the ban cools down) for a few
  *cooldown* cycles, and gives Blinkit (which has no WAF) extra passes as a
  *boost* so coverage doesn't crater — the ledger will show `gate`/`boost`
  events.
- **Alerts**: a price far from the store's own **baseline** (the normal
  price we learn for each SKU at each store from history — deviations from
  it are what trigger glitch alerts) or the **honey-pot** true price. A
  **honey pot / canary** is a handful of products whose TRUE price (honey
  pot) or true in-stock status (canary — e.g. milk, essentially never out
  of stock) we know; if those look wrong, something is wrong with OUR
  crawler, not the store. (Named after canaries carried into coal mines.)
  Crossing `detect.*` thresholds raises an alert: the **deviation %** is how
  far a price sits from baseline/honey-pot true price; the **z-score** is
  statistically how weird a price is relative to that SKU's own history
  (in units of "standard deviations"), catching glitches on items whose
  normal price varies. A price far below **MRP** (Maximum Retail Price, the
  printed ceiling) is either a real sale or a glitch. Interpretation is
  *local* — a glitch at store 34292 says nothing about store 36517.
  Cross-check the link in the app before believing (or buying).
- **Categories panel**: share of observations per product category. If
  "Dairy & Eggs" dominates, that's the apps' **home carousels / category
  rails** (the scrolling product rows and category strip on the front page)
  being ordered **dairy-first** (milk, bread, eggs — what people buy most)
  — raw crawling over-samples milk, which is why the config has
  `skip_categories` and `crawl_terms` de-biasing knobs. Tune them toward
  your basket.

Sanity query: `sqlite3 deals.db "SELECT ts, sku_key, reason, price FROM
alerts ORDER BY ts DESC LIMIT 10;"` (`ts` is a unix epoch-seconds
timestamp everywhere in this repo, not a human date).

## Stage 3 — Price search & the Telegram bot (instant, no Telegram needed)

    python3 run.py --search "cigarettes"       # CLI across QC platforms
    python3 run.py --bot                       # Telegram bot + monitor together

The reply ranks by **effective price** = item + delivery fee + best code
from `codes.yaml`. Interpretation: edit `codes.yaml` to the cards/codes you
actually hold, or the "effective" number is fiction. Offers show their
note so you can verify at checkout.

Bot extras:

- **`/watch <product>`** — "ping me when this shows up cheap in any crawl
  or search". Stored in `keyword_watches`; matched by **token overlap**
  (shared words) scoring (plus the semantic layer below). Rate-capped,
  6h cooldown per watch — **rate caps / cooldowns** are hard limits on
  pushes per hour and per watch, so the bot can't spam you.
- **`/digest`** — today's cheapest per category + DPI top-5, straight from
  the DB, no crawling.

### How matching works: words + meaning (semantic)

Plain word-matching misses "diet coke" vs "Coca-Cola Zero Sugar 750ml", so
searches and watches are also scored semantically:

- **Embedding** — a list of numbers (a **vector**) that a model assigns to
  a piece of text so that texts with similar MEANING get similar vectors,
  even with zero shared words.
- **Cosine similarity** — the standard 0–1 measure of "how close" two
  vectors are (the angle between them). In this repo, live Gemini cosines:
  unrelated products ~0.50, category matches ~0.58–0.66, same product
  ~0.65–0.80.
- **`embeddings` table** — the cache in `deals.db`: one vector per distinct
  product name, keyed (model, dims, name) so different providers live side
  by side (NVIDIA Nemotron @2048, Gemini @768, local embeddinggemma @768);
  additive — nothing else reads it, safe to ignore or wipe.
- **Semantic lift** — scores become `max(token_score, semantic_score)`:
  meaning can only ADD a candidate the word-match missed, never demote one
  that matched. Endpoint down = the old word-only behaviour.
- **Backfill** (`--embed-catalog`) — vectorize every distinct product name
  once. Provider is NVIDIA-hosted Nemotron by default; OPT-IN local
  alternative: `ai.embedding_provider: "ollama"` runs Google's
  embeddinggemma-300m on the machine itself (zero quota, ~8 names/sec on
  this box, batch 256). Resumable — re-run anytime to continue.
- **`--similar <phrase>`** — archive query: which known product names are
  semantically closest to a phrase you type.

## Stage 4 — Demand Radar (the pipeline — strict order)

Three phases build on each other; each writes what the next reads. Times
assume the default config (Andheri West, blinkit+zepto+instamart). The
geography layer first, because everything is per-store: a **locality**
(e.g. Andheri West) is cut into a **grid** of GPS **anchors**; each anchor,
fed to the app, resolves to the nearest darkstore. The **catchment** is
the area one darkstore actually serves; the **rotation pool** is the set of
anchors that resolve to the same darkstore, saved under `exports/` so
mapping doesn't redo all the work.

### 4a. Map the darkstores (once per locality, ~10–30 min)

    python3 run.py --map-locality                  # all apps
    python3 run.py --map-locality --apps blinkit   # or one app

Probes the anchor grid; clusters of anchors = one store's catchment.
Writes the `darkstores` table + `exports/locality_current_<app>.json`
(rotation pools). **Saturation stop**: during mapping, probing stops once
new probes stop finding new stores.

Interpretation: the end summary lists stores with **ETA** (Estimated Time
to Delivery — the "8 min" badge; per darkstore, never per product). A store
id that appears under anchors 800m apart is normal (big catchment).
`products=0` with a resolved store = the app gated the session (see Stage
2's WAF), not that the store is empty.

### 4b. Build the watchlists (once per store set, ~2–5 min/store)

    python3 run.py --build-watchlist
    python3 run.py --build-watchlist --apps blinkit --store 47578

The **watchlist** is the per-store memory of SKUs we care about, so a
product going missing is a signal instead of blindness. Built by a **deep
sweep** — the full crawl pass over a store in ONE browser session: home
feed → category pages (`demand.categories_per_store`, minus any
`skip_categories` labels) → search terms (`demand.staple_queries`). Every
SKU is scored and stored in `watchlist` with its collection labels.

Interpretation: `[watchlist] blinkit/34292: 1834 SKUs seen (1790
stock-stamped, 212 OOS at build) -> active=1834`. "OOS at build" is a
baseline, not news. With `unbiased_harvest: true` everything stays active —
the memory, not the probe list. Re-run after you change `staple_queries`
or `skip_categories`, or when the catalog drifts (weekly is plenty).

### 4c. Probe stock (continuous — this is the one you leave running)

    python3 run.py --demand                        # loop
    python3 run.py --demand --once --max-terms 5   # quick single round (~70s)

The **prober** (`--demand`) re-sweeps a store on a timer (home +
categories + its top watchlist search terms) and writes **`stock_obs`** —
the table of every observation: (store, SKU, in-stock yes/no/unknown,
price, time). The raw evidence.

- **Debounce**: N consecutive zero-reads (`oos_debounce_snapshots`)
  required before an event opens — ignoring single flaky readings so one
  bad page load can't fabricate a stock-out. Only then is an **`oos_event`**
  (confirmed stock-out incident) opened; it closes the moment the item is
  seen in stock again.
- **Streak**: how many consecutive sweeps an SKU has been read
  out-of-stock. **Restart-proof**: the counter is rebuilt from history, so
  restarting the prober doesn't reset it or fake a start time.
- **Vanished**: SKUs missing from M successful sweeps become `vanished`
  events (delisted/hidden — an assortment change, deliberately NOT
  counted as out-of-stock, per golden rule 4).
- **Canaries**: `stock_canary_queries` must return in-stock items — if milk
  shows OOS everywhere, the machine *freezes* (suspect cycle) instead of
  inventing demand.
- **Suspect flip**: if a whole store's items flip in-stock→OOS at once,
  that's almost certainly us being soft-blocked, not the store emptying;
  event changes are frozen for that cycle.

### 4d. Read the results

    python3 run.py --demand-report            # terminal
    python3 run.py --demand-report --csv      # + exports/dpi_<date>.csv

or the dashboard Demand panels. How to interpret each:

- **DPI (Demand Pressure Index)** = Σ OOS-minutes × recency ÷ days
  observed — this repo's stock-out intensity score per SKU. High = keeps
  going out of stock, lately. It is a *proxy for demand* — we see shelves,
  never order volumes. A SKU observed for only two days with one long
  outage can outrank a month of data: check the obs-days column before
  acting.
- **Onset heatmap** (hour-of-day × SKU): where stock-outs *begin* — read
  columns for "evening collapse" patterns; read rows for "this SKU lives
  on the edge". **Blank = prober wasn't running then.**
- **ETA curve**: median delivery minutes per hour per store. Rising ETA
  and rising OOS together = store strain; ETA alone = traffic/rain.
- **oos vs vanished**: `vanished` = delisted/hidden (assortment change),
  `oos` = listed but unavailable (demand or supply). Different stories —
  don't merge them.

Useful queries:

    sqlite3 deals.db "SELECT sku_key, COUNT(*) n FROM oos_events WHERE kind='oos' GROUP BY 1 ORDER BY n DESC LIMIT 10;"
    sqlite3 deals.db "SELECT store_id, COUNT(*), MAX(eta_min) FROM stock_obs WHERE ts>strftime('%s','now')-86400 GROUP BY 1;"

Build order is **phases 1–5**: 1 map stores → 2 build watchlists → 3 probe
stock → 4 analyze (DPI/heatmap/ETA) → 5 hardening (proxies, anchor
rotation; the **exit IP** — the internet address your traffic appears to
come from — can get reputation-blocked by heavy crawling, and proxies
rotate it). Status lives in `DEMAND_RADAR.md`.

## Stage 5 — The AI assistant (optional, after Stage 4 has data)

Dashboard **AI panel** → *Understand the results*. The **LLM** (Large
Language Model — the chat AI answering in the panel) receives a compact
**digest** of the DB (DPI table, heatmap, ETA, counts) — it does not crawl
and only knows what's in that digest plus your question. Two practical
notes learned the hard way:

- Long answers can hit the panel's limits, so every analysis is also saved
  as a downloadable markdown report (`exports/ai_explain_*.md`);
  **follow-up chat** (questions about the last analysis) is appended to
  the same file.
- Free-tier providers rate-limit (**429** = "too many requests"): wait a
  few seconds between analyses.

Config: the `ai:` block is **OpenAI-compatible** — any provider speaking
the same HTTP dialect works via `base_url` + `model` (Gemini does; local
Ollama runs keyless); `AI_API_KEY` in `.env` is the secret. **Tokens** are
an LLM's billing/length unit (~¾ of a word); `max_tokens` caps the answer
(long analyses are sidestepped by saving the full report as a file).

The panel can also *propose* config changes (`/ai/methodology`,
`/ai/focus`) — suggestions are enforced against a whitelist; you approve
before anything is written.

## One-shots worth knowing

    python3 run.py --qc-status          # are the apps serving us at all? (~1 min)
    python3 run.py --store-inventory    # what stores near YOUR real location stock
                                        # (separate inventory_<app>.db files)
                                        # RUN ALONE — stop other crawls first
    python3 scripts/live_sweep.py       # hunt real glitches across the corridor now
    python3 run.py --embed-catalog      # one-time semantic backfill (Stage 3)
    python3 run.py --similar "phrase"   # which known names are semantically closest

All CLI flags, for reference: `--demand` (prober loop), `--demand --once`
(one sweep), `--build-watchlist`, `--map-locality`, `--store-inventory`,
`--search "term"`, `--bot`, `--ui`, `--demo` (offline simulation with an
injected fake glitch), `--qc-status` (one-shot store liveness check), plus
`--check`, `--once`, `--demand-report`, `--embed-catalog`, `--similar` and
the `--apps` / `--store` / `--max-terms` / `--csv` modifiers seen above.

## How the crawler sees anything at all

- **Headless Chromium / Playwright** — a real Chrome browser with no
  window, driven by code (`tools/pw_catalog.js`). The apps can't tell it
  from a phone browser.
- **Browser fingerprint / mobile WebView UA** — the settings (screen size,
  user-agent string) that make the headless browser masquerade as the
  apps' own in-app browser. **UA** = User-Agent, the "who am I" header
  every browser sends.
- **Intercept / mirror** — instead of guessing the apps' private API, we
  let the real page make its own calls and copy the answers out of the
  network traffic.
- **Signed requests / signature** — some apps cryptographically stamp
  every API call (Zepto: `request-signature`, HMAC). We never forge them —
  the browser produces them naturally, we just harvest the responses.
- **Location seeding / request rewrite** — the apps cache "which city am I
  in" aggressively (Blinkit once served us Gurugram from Mumbai). We force
  the target location via localStorage + cookies + rewriting lat/lon
  inside outgoing requests.
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

## The daily rhythm (what to actually leave running)

- **Always-on**: dashboard (`--ui`) + Features ▸ Glitch monitor + Features
  ▸ Demand prober. That's the whole product.
- **Weekly**: re-run `--build-watchlist` (catalogs drift); skim
  `--demand-report` or the Demand panels.
- **When something looks wrong**: `--qc-status` first (is it us or the
  apps?), then the feature's log tail in the panel.

## Symptom → meaning → fix

| Symptom | Meaning | Fix |
|---|---|---|
| `products=0` everywhere, stores resolve | fresh sessions rate-limited (two crawls at once, or IP reputation) | stop all crawls, wait a cycle, restart ONE |
| One app silent for many cycles | WAF-gated; failover is leaning on Blinkit | nothing — cooldown is automatic; check `--qc-status` |
| "suspect cycle" / machine frozen | canaries OOS or mass in→out flip = soft-block suspected | let it ride; if hours long, check IP/proxy |
| Heatmap mostly blank | prober wasn't running those hours | start Demand prober; blanks are not "all fine" |
| DPI dominated by one SKU for days | could be real, or a zombie event from stale coverage | check obs-days + last real sighting in stock_obs |
| AI panel errors | no key / 429 rate limit | check `ai:` config + `.env`; wait and retry |
| Alerts stopped entirely | rate cap (`alert.max_alerts_per_hour`) or monitor down | Features ▸ monitor log |
| Dashboard shows stale numbers | feature restarted but dashboard didn't respawn it… or config not applied | stop/start the feature; refresh |

## Where the data lives (and how it's stored)

Everything is one **sqlite** file — `deals.db` (a single file-based
database): `price_obs` (every price seen), `alerts` (glitches), `darkstores`,
`watchlist` (SKU memory), `stock_obs` (every stock read), `oos_events`
(confirmed stock-outs/vanishings), `searches`/`search_results` (bot
history), `keyword_watches`, `embeddings` (semantic cache). `inventory_*
.db` are separate per-app snapshots from `--store-inventory`.

Database basics: a **table** is a spreadsheet in the DB (rows are records,
columns are fields); the **schema** is the set of tables and columns, and
it is **additive only** — a hard repo rule: never drop/rename, only add,
because other features read it live. A schema **backfill** safely adds a
new column to an existing table by creating it empty and filling old rows.
`ts` columns are unix epoch seconds (floats), not human dates.

Generated artifacts live under **`exports/`** (locality maps, DPI CSVs, AI
report markdown files — gitignored).

The dashboard **SQL databases** panel browses all of it read-only. To start
completely fresh: stop all features, move `deals.db` aside, restart —
history is never required, but every interpretation above needs *some*
accumulation first.

## Repo map shorthand

- **`src/`** — the Python package; one module per feature (see AGENTS.md
  repo map). **`tools/`** — the JS crawler. **`docs/`** — reverse-
  engineering reports on the apps' APIs/APKs. **`scripts/`** — one-off
  tools. **`apks/`** — raw installer files for reverse-engineering
  (gitignored, too big).

**The most useful way to think about the **final Opportunity Engine** is not “find empty spaces on the map.” It is:**

> **Given the products, prices, pack sizes, assortment, stock pressure, and historical catalog behavior we can actually observe, tell me where the market appears underserved and what kind of product/assortment change could address it.**

For this repo specifically, I would expect the finished engine to produce several classes of outputs.

## 1. Cross-app assortment gaps

This is the easiest and highest-confidence use case.

Suppose the engine finds:

```text
Category: Protein bars

Blinkit:       14 relevant SKUs
Zepto:         17
Instamart:      6

Product group:
"Brand X Chocolate Protein Bar 60g"

Blinkit:       ✓
Zepto:         ✓
Instamart:     ✗
```

The engine can say:

> **Assortment gap:** Instamart is missing a product that is established in the comparable Blinkit/Zepto assortment.

That's not necessarily a new-product opportunity. It could be:

**“Stock this existing product.”**

This is exactly why the tailored plan puts assortment gaps before the more speculative geometric gaps. 

---

# 2. Price/pack-size gaps

This is probably one of the most interesting opportunities for your actual grocery dataset.

Imagine the engine discovers:

```text
Detergent

₹40 / 250g
₹65 / 500g
₹120 / 1kg
₹210 / 2kg
```

and there is a strong concentration around those products, but almost nothing around:

```text
~750g
~₹85
```

The engine could produce:

> **Potential pack-size gap**
>
> The category has established products below and above this point, but very little assortment around the 750g / ₹80–₹90 position.

This is much more actionable than “there's an empty part of the UMAP.”

Your M1 design specifically introduces normalized pack size and `unit_price`, which makes this kind of analysis possible. 

---

# 3. Unit-price gaps

This may actually be even more useful than raw price.

Imagine:

```text
Protein snack category

₹8 / 10g
₹9 / 10g
₹11 / 10g
₹15 / 10g
```

and almost nothing around:

```text
₹12 / 10g
```

The engine could identify:

> A missing mid-premium price position.

That can suggest:

```text
same category
same general product format
different price/value proposition
```

rather than blindly recommending a completely new category.

This is one reason I think `unit_price` is going to become a surprisingly important feature in your system.

---

# 4. Brand-tier gaps

Suppose the engine sees:

```text
Budget brands
████████████

Premium brands
████████████

Mid-premium
██
```

while the category itself has:

* substantial catalog density
* strong neighboring DPI
* healthy availability
* established demand

It could identify:

> **Brand-position gap:** strong category presence with weak representation in the mid-premium price/value tier.

That doesn't necessarily mean:

> launch a new SKU.

It could mean:

> introduce a private-label or mid-tier brand.

---

# 5. Demand-backed assortment gaps

This is where your existing Demand Radar becomes very valuable.

Imagine:

```text
Category:
Chocolate protein bars

Nearby products:

Product A
DPI: very high

Product B
DPI: high

Product C
DPI: high
```

But a related segment has almost no assortment.

The engine could produce:

> **Demand-backed assortment gap**
>
> Products in this semantic/price neighborhood exhibit elevated stock-out pressure, while assortment in the adjacent segment remains thin.

That's much stronger than:

> "This region is sparse."

Because now you have:

```text
gap
+
market density
+
observed stock pressure
```

The repo's DPI is explicitly designed as a demand-pressure proxy rather than direct sales data, so the output should phrase this carefully. 

---

# 6. “What product should we stock?” rather than “what product should we invent?”

This is perhaps the most immediately valuable practical use case.

Suppose:

```text
Product group X

Zepto:       widely present
Blinkit:     widely present
Instamart:   absent
```

The Opportunity Engine can effectively become an **assortment recommendation engine**:

```text
Recommendation:
Add Product X to Instamart assortment

Evidence:
- present on 2 competing apps
- comparable stores covered
- category has sufficient coverage
- product observed repeatedly
- no crawl anomaly detected
```

This is a very defensible first commercial use case.

---

# 7. “What are competitors carrying that we aren't?”

This is a natural extension.

The system could generate:

```text
                  ASSORTMENT GAP REPORT

Instamart vs Blinkit

Largest missing product groups:

1. Product A
2. Product B
3. Product C
4. Product D
```

Then rank them by:

```text
DPI around category
coverage
cross-app prevalence
catalog persistence
```

So the system isn't just a map anymore.

It's answering:

> **What should this app consider adding?**

---

# 8. Local assortment gaps

This gets particularly interesting because your system works at darkstore/store level.

Suppose:

```text
Product X

Store A: ✓
Store B: ✓
Store C: ✗
Store D: ✗
```

and the missing stores are within the same general market.

The engine could say:

> **Local assortment gap:** product is established in nearby stores but absent from this store cluster.

That is different from a product-development opportunity.

It's a **distribution / inventory-assortment opportunity**.

---

# 9. Persistent gaps vs temporary gaps

This is an underrated use case.

The system could discover:

```text
Gap observed:
Week 1 ✓
Week 2 ✓
Week 3 ✓
Week 4 ✓
Week 5 ✓
```

versus:

```text
Gap observed:
Week 1 ✓
Week 2 ✗
Week 3 ✗
Week 4 ✓
```

The first might be:

> **structural assortment gap**

The second might just be:

> temporary stock/coverage issue.

Because the repository already tracks catalog snapshots and catalog events, the final engine can make that distinction. 

---

# 10. Emerging product segments

This is where the system starts becoming more strategic.

Imagine the product space changes like this:

```text
Month 1   ● ●
Month 2   ● ● ●
Month 3   ● ● ● ● ●
Month 4   ● ● ● ● ● ● ● ●
Month 5   ● ● ● ● ● ● ● ● ● ●
```

The engine notices a previously tiny semantic region expanding rapidly.

It could report:

> **Emerging segment detected**

with:

```text
products: +240%
new brands: +5
new SKUs: +18
DPI: rising
apps carrying category: increasing
```

That isn't necessarily a "gap."

It's a **trend/opportunity signal**.

---

# 11. Product-space “white space”

This is the closest to the original idea you had.

Imagine:

```text
                 Premium
                    ●
                 ● ● ●
              ● ● ● ● ●
            ● ● ○ ● ●
              ● ● ●
                 ●

                  ↑
             candidate gap
```

The system could identify a region that is:

* inside a well-populated category
* adjacent to established products
* sparse relative to its neighborhood
* supported by meaningful attribute variation

and produce:

> **Potential product-space white space**

For example:

```text
Category:
Ready-to-drink protein beverages

Observed:
₹80–₹120
200–250 ml

Observed:
₹150–₹180
300–350 ml

Gap:
₹110–₹140
300 ml
```

This could lead to:

> Potential mid-price / larger-pack product configuration.

But this should have **lower confidence** than a directly observed assortment gap.

---

# 12. “What combination is missing?”

This is the genuinely exciting long-term version.

Suppose the category has:

```text
small + cheap
small + premium
large + premium
```

but almost no:

```text
large + mid-price
```

The Opportunity Engine can construct:

```text
candidate:

larger pack
mid-range unit price
same general category
```

and then search the semantic neighborhood to see whether anything equivalent already exists.

That starts to approach:

> **new product concept discovery**

rather than assortment optimization.

---

# 13. Opportunity prioritization

Eventually you won't want:

> 4,000 detected gaps.

You'll want:

```text
TOP OPPORTUNITIES

#1  Add existing Product X to App Y
    Score: 91
    Confidence: A

#2  Mid-price / larger-pack segment in Category Z
    Score: 82
    Confidence: B

#3  Emerging subcategory with weak assortment
    Score: 77
    Confidence: B
```

This is where the Opportunity Engine becomes a **decision-support layer**.

---

# 14. The really interesting combined output

I think the most valuable final output will combine multiple signals.

For example:

```text
OPPORTUNITY #17

Category:
Protein Snacks

Candidate:
Mid-priced 4-pack protein bars

Why flagged:

Semantic space
──────────────
Dense surrounding market
Sparse candidate region

Attributes
──────────
Existing:
₹45–₹65 single bars
₹110–₹150 large packs

Missing:
~₹80–₹100 / multipack position

Market
──────
3 apps carry adjacent products
1 app has no equivalent

Demand
──────
Neighbor DPI: High

History
───────
Gap persists for 5 weekly snapshots

Coverage
────────
Good

Validation
──────────
No equivalent current product found

Score
─────
86 / 100

Confidence
──────────
B+
```

Now that's something a human could actually act on.

---

# 15. There are really three products hiding inside the Opportunity Engine

I would think of the final system as having three increasingly ambitious layers.

### Level 1 — Assortment Intelligence

> **What products should this app/store carry that it currently doesn't?**

High confidence.

Uses:

```text
cross-app identity
catalog coverage
store coverage
catalog persistence
DPI
```

### Level 2 — Market White-Space Intelligence

> **What product/price/pack-size regions appear underserved?**

Medium confidence.

Uses:

```text
semantic embeddings
unit price
pack size
density
neighbors
DPI
temporal stability
```

### Level 3 — Product Concept Discovery

> **What plausible product configuration appears to be missing from the market?**

Lower confidence, but potentially much more valuable.

Uses:

```text
semantic space
attribute space
interpolation
hypothetical candidate generation
external existence validation
LLM interpretation
```

The current repo should absolutely build them in that order.

---

## The final Opportunity Engine, visually

I would ultimately want the Atlas to let you switch from:

```text
EXPLORE
```

to:

```text
OPPORTUNITIES
```

and see something like:

```text
                  ● ● ●
              ● ● ● ● ● ●
            ● ● ● ◎ ● ● ●
              ● ● ● ● ●
                 ◉

        ◎  Candidate gap
        ◉  High-confidence opportunity
        ●  Existing product
```

Click `◉`, and the inspector tells you:

> **Why is this an opportunity?**

not just:

> "This point is located here."

That's the fundamental distinction between the current Inventory Atlas and the eventual **Product-Space Intelligence Engine**.

And importantly, the current repo-specific plan is already moving toward exactly this hierarchy: **assortment gaps first, then density/attribute gaps, then DPI/churn scoring, validation, and finally AI interpretation.**
