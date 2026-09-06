# qcom-scraping v2 — Glitch & Deal Monitor

## The core problem we are solving

Telegram groups are a *downstream* firehose: by the time a "Blinkit loot" or
"Instamart glitch" hits a channel, thousands of people already saw it and the
price is usually corrected. The edge is being **upstream of the groups** — i.e.
watching the apps *yourself*, in the first minutes a bad price is live.

But the apps actively fight server-side polling:

- **Blinkit** returns `403` to non-app traffic.
- **Swiggy Instamart** returns `202` with an empty body (soft-block / JS challenge).
- **Zepto** returns `404`/challenge to guessed API paths.
- All three ship their real catalog through **app-only, versioned, signed**
  endpoints whose paths rotate and require device/install tokens + checksum
  headers that change per build.

So a naive `requests.get(api_url)` architecture is dead on arrival. The design
below is built around that reality.

---

## Architecture overview

```
                         ┌─────────────────────────────────────────┐
                         │            ORCHESTRATOR (scheduler)      │
                         │  staggered, jittered, per-source cycles  │
                         └───────────────┬───────────────┬──────────┘
                                         │               │
                       ┌─────────────────▼───┐       ┌───▼─────────────────┐
                       │  SOURCE ADAPTERS    │       │  GEO / STORE LAYER  │
                       │ (anti-block beasts) │       │ Mumbai Virar→Andheri│
                       └─────────┬───────────┘       └─────────┬───────────┘
                                 │ feeds normalized            │ constrains
                                 │ product stream              │ which stores
                                 ▼                             │ to watch
                       ┌───────────────────────┐              │
                       │   ANOMALY DETECTOR    │◄─────────────┘
                       │ price-glitch scorer   │   (per-location baseline)
                       └───────────┬───────────┘
                                   │ flagged deals
                                   ▼
                       ┌───────────────────────┐
                       │   ALERT FAN-OUT        │
                       │ desktop / tg / log     │
                       └───────────────────────┘
```

### Why this shape
- **Adapters own the anti-block tricks** so the rest of the pipeline stays clean.
- **Geo layer is first-class**, not an afterthought: quick-commerce prices are
  *per-dark-store*, so a glitch in Andheri may not exist in Borivali. We watch
  a corridor of stations and treat each store as its own baseline.
- **Anomaly detector is per-store, per-SKU** so "cheap in one locality" isn't
  flagged as a glitch when it's just a local promo.

---

## The creative bypass layer (how we beat polling limits)

We do **not** depend on a stable server-side API. Instead we combine:

1. **Headless-app emulation, not web scraping.**
   Run the *actual* mobile web app in a real browser engine (Playwright with a
   mobile UA + a real Blinkit/Zepto/Swiggy WebView fingerprint). The JS
   challenges that block `curl` are solved by the browser itself. Cookies,
   install-id, and the rotating API token are produced naturally by the page.

2. **Request interception mirror.**
   While the headless browser runs the real app, we *intercept* its XHR/fetch
   calls to the catalog API. We don't guess the endpoint — the app tells us the
   current path + signed headers. We replay those exact requests (cheap, fast,
   no full page reload) on a tight loop. When the app rotates the path, the next
   page load re-discovers it. This is the key trick: **the app is the API
   documentation.**

3. **Geo-anchored store hopping.**
   Instead of hammering one store, we walk the Mumbai corridor (Virar → Andheri)
   by setting `lat/lon` per station, which makes each dark store resolve
   separately. A glitch often appears in *one* store first — location-walking
   finds it before it spreads.

4. **Distributed identity (proxy + UA + install-id rotation).**
   Each cycle uses a fresh mobile UA, a rotated install/device id, and (if
   configured) a residential proxy. Combined with jitter, this defeats simple
   rate fingerprints without needing a paid datacenter farm.

5. **Honey-pot SKU basket.**
   We maintain a small fixed basket of high-velocity, normally-priced SKUs
   (e.g. 1L Amul milk, Lay's 90g, Maggi 12-pack). Because we know their *true*
   price, any deviation is an instant glitch signal — no ML needed for the
   first-pass filter. Everything else goes through statistical anomaly scoring.

6. **Quiet-period differential crawling.**
   We crawl *more* during off-peak (server-side rate limits are looser at 3am)
   and rely on the honey-pot basket to catch corrections. This inverts the usual
   polling pattern so we look less like a bot burst.

---

## Mumbai corridor — Virar to Andheri

Stations (west + harbour mix along the line we care about), each with a
representative lat/lon used to resolve the nearest dark store:

| Station     | Lat      | Lon       |
|-------------|----------|-----------|
| Virar       | 19.456  | 72.806    |
| Vasai Road  | 19.392  | 72.805    |
| Nalasopara  | 19.413  | 72.790    |
| Mira Road   | 19.282  | 72.857    |
| Bhayandar   | 19.231  | 72.851    |
| Goregaon    | 19.166  | 72.852    |
| Andheri     | 19.119  | 72.846    |

(Plus a few extras: Malad, Borivali, Kandivali, Dahisar.) The geo layer pings
each app's location endpoint with these coords to resolve `store_id`s, then
crawls the basket per store.

---

## Demand Radar (added 08-22) — per-darkstore stock-out intelligence

A second mission running on the same browser-intercept crawler. Stock state
and delivery ETA are PER DARK STORE, so the pipeline is locality-first:

    --map-locality      anchor grid + landmark seeds -> resolve & cluster the
                        locality's darkstores (darkstores table +
                        exports/locality_<name>.json with rotation pools)
    --build-watchlist   ONE browser session per store: home harvest -> DOM
                        category click-through -> staple searches; every SKU
                        scored (search hits > home) into the watchlist table
    --demand            continuous loop: re-sweep a store's watchlist
                        collections, write stock_obs snapshots, run the
                        debounced oos_events machine ('oos' | 'vanished'),
                        guarded by stock canaries + mass-flip soft-block
                        detection (suspect cycles freeze the machine)
    (phase 4 TODO)      Demand Pressure Index rollups + hour-of-day heatmap

Two discoveries shaped the design (full story: DEMAND_RADAR.md, AGENTS.md):

- **QC apps cache their serving location client-side** and ignore spoofed
  GPS — Blinkit silently served a Gurugram store until the crawler learned to
  seed localStorage/cookies AND rewrite lat/lon on outgoing signed requests.
- **Order volumes are never exposed by any app**; stock-out intensity
  (frequency × duration × hour) is the honest proxy for demand.

## Files

```
qcom-scraping/
  ARCHITECTURE.md        this file
  README.md              how to run
  AGENTS.md              agent/operator onboarding: quirks, invariants,
                         multi-agent coordination rules — read before editing
  DEMAND_RADAR.md        demand-radar design + live phase status
  config.yaml            all tunables (NO secrets — those go in .env)
  codes.yaml             delivery fees + offers knowledge base (user-editable)
  .env.example           template for tokens
  run.py                 entrypoint (loop / --once / --check / --demo /
                                    --search "q" / --bot / --ui /
                                    --map-locality / --build-watchlist /
                                    --demand)
  tools/
    pw_catalog.js        Node+Playwright browser-intercept crawler (the bypass;
                         deep-sweep visit queue, location seeding + rewrite,
                         DSH_BODY_DIR raw-body dump hook)
  scripts/
    live_sweep.py        one-shot real-glitch hunt across stations
  exports/               locality mapping JSON (per-store rotation pools)
  src/
    orchestrator.py      scheduler + cycle control
    geo.py               mumbai stations + store resolver
    locality.py          demand phase 1: anchors + darkstore discovery
    watchlist.py         demand phase 2: per-store SKU probe sets
    prober.py            demand phase 3: stock_obs loop + oos_events machine
    categories.py        keyword product-category classifier
    store.py             sqlite schema + persistence (price_obs, alerts,
                         darkstores, watchlist, stock_obs, oos_events,
                         searches, search_results)
    search.py            cross-platform price search engine (parallel fan-out)
    pricing.py           effective price: delivery fees + best code/offer
    tgbot.py             stdlib Telegram bot (long-polling)
    dashboard.py         live status UI (+ /searches, /categories endpoints)
    events.py            thread-safe event bus feeding the dashboard
    alert.py             desktop + telegram + log fan-out
    detect.py            anomaly / glitch scorer
    honey.py             honey-pot SKU basket
    miniyaml.py          stdlib YAML-subset parser (zero deps)
    adapters/
      base.py            adapter interface; shells out to tools/pw_catalog.js
      instamart.py       swiggy instamart
      blinkit.py         blinkit
      zepto.py           zeptonow
      amazon.py          amazon.in search (DOM extraction)
      flipkart.py        flipkart search (DOM + text extraction)
      trackers.py        amazon/flipkart price-track
      demo.py            simulated source w/ injected glitch (--demo)
```

### Search feature flow

```
 you (Telegram) ──► tgbot.py ──► SearchEngine.search(query)
                                    │  ThreadPool: all 5 platforms AT ONCE
                                    ▼
        blinkit / zepto / instamart / amazon / flipkart adapters
             (browser-intercept: JSON first, DOM/text fallback)
                                    │
                                    ▼
                     fuzzy match (token overlap ≥ 0.5)
                                    │
                                    ▼
              pricing.py: listed − best code/offer + delivery fee
                                    │
                                    ▼
                  ranked cheapest-first reply to Telegram
```

`--bot` runs the glitch monitor loop in a background thread while the bot
polls Telegram in the foreground — both features operate simultaneously.
Bare `--ui` is dashboard-only (no loops); add `--bot` and/or `--monitor`
to run them alongside the dashboard, or start loops from the Features panel.

> The browser layer is driven through the **pre-installed Node Playwright**
> (`../Do not delete folder/node_modules` + `.pw-browsers`, wired in
> `config.yaml`). If Node or the browsers are missing, adapters degrade
> gracefully and the rest of the pipeline keeps running.
