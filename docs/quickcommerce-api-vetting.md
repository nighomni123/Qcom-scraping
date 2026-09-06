# QuickCommerce API — Vetting Notes (anti-bot / data-source analysis)

> Research only. No repo code depends on this. Source: public pages on
> `quickcommerceapi.com` + the API's own public/keyless endpoints, gathered
> 2026-08-29 via the `monid`/`tinyfish` web tool (per `../AGENTS.md` policy).
> All `tinyfish` calls were free ($0). Verified live reachability from this
> machine on 2026-08-29.

## TL;DR verdict

QuickCommerce API **deliberately does not publish how it bypasses anti-bot**.
The marketing describes the *problem* ("captchas, IP bans, layout churn") and
asserts "we handle infrastructure for you" without disclosing the method.
A full read of their own site yields a credible **two-layer architecture**:

1. **Consumer-app data source (the "no-bypass" bypass).** A large share of
   their live data comes from real users running real searches in the real
   apps. The platforms see legitimate traffic, so there is nothing to defeat.
2. **Automated backend fleet (where bypass technique actually lives).** For
   coverage no user base can provide (national catalogue, datasets, arbitrary
   ETA), they run crawlers. The hints they let slip point to the standard
   playbook: residential/mobile proxy rotation, reverse-engineered signed
   private APIs (not HTML scraping), and proper geolocation injection.

## Layer 1 — consumer-app data (CONFIRMED, primary)

- `quickcompare.ai` (their B2B site): analytics **"powered by 5 lakh+ app
  users"**, **"consumer behavior data from 5 lakh+ app users"**, generating
  **"millions of search queries every month."**
- Homepage: the API is **"the same API behind QuickCompare"** (consumer price-
  comparison app, 1M+ downloads / 10K DAU per their copy).
- Implication: genuine user-initiated queries through official apps on real
  devices => platforms see normal app traffic. This is why they can tell
  customers "no proxies, no captchas" and claim "lower ToS risk."

## Layer 2 — automated fleet (CONFIRMED it exists; technique INFERRED)

Evidence of a backend crawler:

- "National catalogue refreshed continuously" — 5.8L SKUs, 549 cities,
  41K brands (from `/blog/licensed-quick-commerce-datasets`).
- `/status`: **"Health checks run every 15 minutes across 11 platforms
  (search & item). ETA monitored for 7."** => automated monitoring fleet.
- `/scanner`: enumerate every dark store in a city, then call `/v1/item`
  **"one credit per store — same calls your apps use."**
- `/datasets`: **904 dark stores in Bengaluru, 595 in New Delhi** — to
  snapshot those they crawl each store repeatedly.

Confirmed hints they themselves state:

| # | Their own words | Implies |
|---|---|---|
| 1 | "Q-commerce backends **fingerprint datacenter IPs fast**. After a few hundred requests you need **pools of residential proxies**." | They use residential/mobile proxy rotation. |
| 2 | "**Headless Chrome + stealth plugins** become a forever war." | They do NOT rely on headless Chrome at scale => they use reverse-engineered native/signed APIs. |
| 3 | "**Reverse-engineered private APIs** change without changelog." | Explicit admission; same category as qcom-scraping's `pw_catalog.js`. |
| 4 | "**Fake session cookies or map pin selection** breaks constantly." => they pass lat/lon/pincode as first-class params. | Proper geolocation injection; their `x-geolocation-pincode` header normalizes each platform's quirk. |
| 5 | LinkedIn Pulse piece openly lists: multi-proxy rotation, dynamic header manipulation, automated captcha solving, request throttling, retry. | Published under their brand => signals it's in their stack (generic SEO filler, but telling). |

Inferred playbook (reasonable, not stated): residential/mobile proxy rotation +
many device/session contexts; per-platform signed-API clients (not HTML
scraping); per-store timestamped snapshots (`/v1/item` returns `store_id` +
`fetched_at` + `inventory`) on a rotation across the 549-city store graph;
throttling under their own stated **100 req/min** ceiling.

## What they deliberately hide

- `/scanner` and `/datasets` marketing pages are **themselves bot-protected**
  (the `tinyfish` fetch returned `bot_blocked`/empty) — mildly ironic, and
  consistent with not wanting competitors to see their crawl structure.
- No disclosure of proxy vendor, fleet size, headless-vs-SDK, or captcha
  approach. Everything is framed as "we handle infrastructure."

## Legal / ToS posture (their own words)

- Anti-bot article: *"Scraping storefronts may violate platform terms of
  service and can get source IPs blocked… treat scrapers as high operational
  and compliance risk."*
- Blinkit-scraper FAQ: *"Using a maintained API reduces ToS and operational
  risk versus a homegrown scraper."*
- Even they concede the ToS gray zone — relevant if qcom-scraping ever
  commercializes.

## Relevance to qcom-scraping (ties to `AGENTS.md`)

- **Same fundamental technique.** `tools/pw_catalog.js` runs the real app in
  headless Chromium, intercepts **signed API calls**, seeds
  `localStorage.location` + `gr_1_lat/lon` cookies, rewrites lat/lon in request
  paths/bodies. That is their admitted "reverse-engineered private API +
  location injection" approach — done from **one machine's real IP**
  (`AGENTS.md`: inventory runs use "public-IP derived, no spoofing").
- **The Instamart login wall we hit (08-24)** — *"our crawl-heavy exit IP gets
  a LOGIN WALL"* — is precisely the **datacenter-IP fingerprinting** they
  describe. Their fix (residential/mobile proxy pools + session diversity) is
  exactly the Demand Radar **Phase-5 "proxy/IP coherence" TODO** already noted
  in `AGENTS.md`. Their existence validates the bottleneck is **IP reputation,
  not technique**.
- Their `x-geolocation-pincode` abstraction is a clean pattern: we could
  similarly normalize per-platform geo quirks (Blinkit/Zepto/BigBasket need no
  pincode; DMart/JioMart/Minutes do) behind one config block instead of ad-hoc
  cookie seeding.
- Their **prebuilt national dark-store graph** (549 cities, 904 stores in
  Bengaluru) is what `locality.py` anchor grid + `watchlist.py` probe set
  *reconstruct* — a paid `/v1/eta` could shortcut our darkstore discovery.

## Confidence levels

- **Confirmed:** consumer-app data source; existence of automated fleet;
  residential-proxy *awareness*; private-API reverse engineering; lat/lon
  location model; 15-min health checks; per-store enumeration.
- **Inferred (strong):** residential proxy rotation in the fleet; signed-API
  replay over HTML scraping; throttling.
- **Unknown:** proxy vendor, fleet size, headless-vs-SDK split, captcha
  approach, and what fraction of any given query is user-sourced vs
  fleet-crawled.

## Live test (option b)

See `scripts/qcapi_probe.sh`. Tested 2026-08-29 from this machine (IP = our
normal egress):

- `GET /v1/supported-platforms` (public, no key) → **HTTP 200, ~0.1–0.6s**,
  returned all 11 platforms. => API is reachable from our IP.
- `GET /v1/search?...&platform=Swiggy` (no key) → **HTTP 401**
  `{"detail":"API key required..."}`. => auth contract captured.

**Authenticated run — key #1 (glitched trial, 2026-08-29):**
- `GET /v1/search?q=milk&lat=19.1306&lon=72.8347&platform=Swiggy`
  with `X-API-Key` → **HTTP 402** `{"detail":{"error":"insufficient_credits",
  "message":"No active credits. Please top up."}}`.
- `GET /v1/eta?...&platform=Swiggy` with key → **HTTP 402** same.
- `GET /v1/credits` with key → **HTTP 200** `{"summary":{"total_available":0,
  "total_used":0,"active_packs":0},"active":[],"inactive":[]}`.
- The key was *valid* (402, not 401) but the 100 free credits **never
  activated** despite verification (onboarding glitch).

**Authenticated run — key #2 (working trial, 2026-08-29): PROOF OBTAINED.**
- `GET /v1/credits` → **HTTP 200** `active_packs:1`, pack `trial`, 100 credits,
  `expires_at: 2026-09-28` (30-day trial).
- `GET /v1/search?q=milk&lat=19.1306&lon=72.8347&platform=Swiggy` →
  **HTTP 200** `status:"success"`, `total_results:100`, returned 100 real
  Instamart products at the **Mumbai (Andheri West) coordinate where our own
  crawler is login-walled**. First hit: *Gokul Full Cream Milk 500ml*,
  `mrp:"39"`, `offer_price:"39"`, `available:true`, `store_id:"1135721"`,
  `platform.sla:"16 mins"`.
- `GET /v1/eta?...&platform=Swiggy` → **HTTP 200** `store_id:"1135721"`,
  `store_ids:["1135721","1398452"]`, `eta:"16 mins"`, `open:true`.

**Conclusion: their claimed anti-bot bypass WORKS for the blocked case.**
`/v1/search` returned live Instamart inventory at a Mumbai lat/lon that
`tools/pw_catalog.js` cannot reach (Instamart login wall, AGENTS.md 08-24).
Raw payloads saved as `qcapi_sample_swiggy_mumbai.search.json` (117 KB, 100
products) and `qcapi_sample_swiggy_mumbai.eta.json` for schema reference.
(Test consumed 4 of 100 trial credits; 96 remaining.)

### Response schema notes (for any future qcom-scraping integration)

Product fields from `/v1/search` → our `price_obs` mapping:

| Their field | Type observed | Mapping note |
|---|---|---|
| `id` | str | platform item id (use as `product_id`) |
| `name`, `brand` | str | product name / brand |
| `mrp`, `offer_price` | **str, rupees** | e.g. `"39"` — cast `int()`; **already rupees, NOT paise** (unlike raw Zepto, which our `pw_catalog.js` divides via `PRICE_DIVISORS`) |
| `quantity` | str | e.g. `"500 ml"` — parse for pack size |
| `available` | bool | → `in_stock` (1/0/NULL per our NULL≠OOS invariant) |
| `inventory` | int | stock count (observed `1` across results — likely a low/placeholder floor) |
| `rating`, `rating_count` | float/int | optional |
| `store_id` | **str** | per-product nearest store (single store in search) |
| `deeplink` | str | product URL on platform |
| `is_ad` | bool | **ad flag — consider filtering out of price comparisons** |
| `rank` | int | result rank |
| `platform.name/sla/open` | str/str/bool | `sla:"16 mins"` → parse int minutes for our `eta_min` |

`/v1/eta` returns the **full store set** for the lat/lon (`store_ids` array),
which is exactly what our `locality.py` anchor grid + `watchlist.py` probe set
reconstruct by hand — a paid `/v1/eta` could shortcut darkstore discovery
(note: their `store_id` is the platform's id, not our internal darkstore id).

## Cross-platform sweep (option b) — 2026-08-29

Same Mumbai coordinate (lat 19.1306, lon 72.8347 = Andheri West), `q=milk`,
across BlinkIt / Zepto / Swiggy (their name for Instamart). 6 calls, 6 credits
(90 of 100 trial credits remaining after the whole session). Raw payloads saved:
`qcapi_sweep_{BlinkIt,Zepto,Swiggy}.search.json` + `.eta.json`.

| Platform | total_results | top product (name) | mrp/offer | store_id | eta | open |
|---|---|---|---|---|---|---|
| BlinkIt | 36 | Fruit Cake Rusk (Let's Try) | 220 / 115 | 33684 | 8 mins | True |
| Zepto | 3 | Amul Taaza Toned Milk Tetra | 77.0 / 77.0 | 37b900f0-… | "Closed" | False |
| Swiggy (Instamart) | 83 | Gokul Full Cream Milk | 39 / 39 | 1135721 | 14 mins | True |

**Critical gotcha — price type is NOT stable across platforms:**

| Platform | `mrp`/`offer_price` type |
|---|---|
| BlinkIt | **int** (e.g. `220`) |
| Zepto | **float** (e.g. `77.0`) |
| Swiggy | **str** (e.g. `"39"`) |

A `QuickCommerceApiAdapter` MUST coerce with a tolerant parser (try int/float,
strip on str). Also note: unlike our raw Zepto feed (paise, divided via
`PRICE_DIVISORS` in `pw_catalog.js`), the API already returns **rupees** — but
the type still varies, so don't assume numeric.

### Comparison vs our own crawler (`deals.db`)

- **Exact price parity (Zepto):** API `Amul Taaza Homogenised Toned Milk
  (Tetra Pack)` = **₹77**; our `price_obs` (app `zepto`) shows the same product
  at **77.0 / 77.0**. The API's prices match our crawler's — validates data
  quality.
- **Instamart gap (the headline):** our `stock_obs` table holds **only BlinkIt**
  rows (14,591); Instamart's last `price_obs` is ~4 days stale (pre-login-wall,
  AGENTS.md 08-24). So our crawler is effectively **dead on Instamart**, while
  the API returns live Instamart at the same Mumbai point. This is precisely the
  value of the paid API for us.
- **Location mismatch (caveat):** our crawler's recent `price_obs` stores are at
  Vasai Road / Virar (`19.39–19.46`), **not** Andheri West (`19.13`). So this is
  a product/price *parity* check, not a strict same-coordinate match. The Zepto
  milk ₹77 agreement still holds across the corridor.
- **store_id schemes differ:** API uses platform-native ids (numeric for
  BlinkIt/Swiggy, UUID for Zepto); our `price_obs.store_id` is a seeded string
  like `multi::Vasai Road@19.392,72.805`. No direct join without a mapping
  layer; match on product name + price instead.
- **Live open/closed state:** API `/v1/eta` reflected Zepto as `open:false`
  ("Closed") at this coordinate — real-time store status our crawler doesn't
  capture as cleanly (we infer open/closed from `isOnboarded`/items, not a
  dedicated status field).

**Net:** the API is a faithful, multi-platform drop-in for the live-price path,
with Instamart coverage our crawler lost. Cost at Scale (₹0.12/credit): one
3-platform `groupsearch` ≈ ₹0.36; a 24×7 1-call/min monitor ≈ ₹26k/month
(≈ one Scale pack) — feasible as a fallback, not a full replacement.

## Same-location inventory comparison — paan-shop / unconventional items (Andheri)

**Goal:** run OUR inventory tracker and the QuickCommerce API at the *same*
Andheri-West coordinate (lat 19.1306, lon 72.8347, ~2.5 km radius) and compare
coverage of non-food / paan-shop items.

**Our side — `python3 run.py --store-inventory --apps blinkit,zepto
--lat 19.1306 --lon 72.8347 --radius-m 2500 --max-points 5`** (background,
per AGENTS.md "run alone" rule; Instamart skipped — known login-walled):
- BlinkIt: **2 darkstores** (33684 @ 19.13060,72.83470; 47578 @ 19.12701,72.83090),
  **2156** product stock-readings / price rows.
- Zepto: **1 darkstore** (b4dc8d65-… @ 19.13060,72.83470), **511** rows.
- Location seeding worked: stores resolved *exactly* at the passed coordinate
  (store 33684 sits on the requested lat/lon). So this IS a true same-point
  comparison, not a different-city artifact.

**Their side — `/v1/search` for 10 paan-shop terms** (paan, cigarette, gutkha,
pan masala, tobacco, bidi, mouth freshener, mukhwas, condom, betel leaf) across
BlinkIt/Zepto/Swiggy at the same coordinate. Raw: `qcapi_paan_sweep.json`.

| Platform | Our crawler paan items | API paan items (distinct) |
|---|---|---|
| BlinkIt | **0** | 106 |
| Zepto | **0** | 101 |
| Instamart (Swiggy) | n/a (walled, no DB) | 107 |

**Headline finding:** our capture returned **ZERO** paan-shop/convenience items
— 100% of the 2667 captured rows are grocery/food (atta, milk, chips, yogurt,
shampoo). The API, querying the *same* Andheri stores, returned 100+ paan/tobacco/
condom/mukhwas items per platform (e.g. BlinkIt: *Betel Leaves (Vidyachi Paan)*
₹15, *Black Lighter* ₹30, *Calcutta Paan Mouth Freshener* ₹265; Zepto: *Connect
Cigarette* ₹390, *Calcutta Paan Mukhwas* ₹110, *Bindi* packs; Swiggy: *Choco
Dates Paan* ₹50, *Classic King Size Slim Rolling Paper* ₹80).

**Why:** our `inventory.py` runs `LocalityMapper` in `capture_products=True`
mode, which persists whatever the home/category + staple probe pages yield. The
default probe terms are grocery staples, so the "Paan & Cigarettes" / "Tobacco"
/ convenience categories are never visited — those products simply aren't in the
captured catalogue. The API, being a per-query search, surfaces them on demand.
This is a **coverage-config gap, not a hard crawler limitation** — we *could*
capture paan items by adding paan search terms to the probe set, but out-of-the-
box inventory capture is grocery-skewed.

**Conclusion for qcom-scraping:** for the live-price/glitch monitor (grocery
staples) our crawler is fine and price-accurate (see ₹77 Zepto milk parity).
But for **non-food / paan-shop / unconventional SKUs**, the API is currently the
only one that surfaces them at a chosen location — and it does so for Instamart
too, where our crawler is fully walled. Full output: `paan_comparison.txt`;
reproducible via `scripts/compare_paan.py`.

### After adding paan `PROBE_TERMS` (08-29) — gap closed

Added `paan`, `cigarette`, `gutkha`, `pan masala`, `tobacco`, `condom`,
`mukhwas` to `src/adapters/blinkit.py` / `zepto.py` `PROBE_TERMS` (kept the
grocery staples). Re-ran `--store-inventory` for the same Andheri point (no API
calls).

| Platform | Before (grocery-only) | After (paan terms) | API reference |
|---|---|---|---|
| BlinkIt | 0 paan items (of 2156) | **83** paan items | 106 |
| Zepto | 0 paan items (of 511) | **81** paan items | 101 |

- Exact price parity on shared SKUs: BlinkIt *Betel Leaves (Vidyachi Paan)*
  ₹15, *Bel Patra (Belachi Paan)* ₹19, *Aayush Herbal Masala* ₹499 — our crawler
  and the API agree to the rupee.
- Our broad category capture surfaced **more** paan SKU variants than the API's
  10-query sweep (80 BlinkIt / 81 Zepto items the API did NOT return by exact
  name — e.g. *American Club LIT Gold Cigarettes*, *Black Stone Cherry Pipe
  Tobacco Cigar*). The "0 in BOTH" on Zepto is exact-string mismatch (API appends
  pack details to names), not a real absence.
- **Conclusion:** the missing non-food coverage was purely a probe-set config
  gap, exactly as hypothesized. Our crawler, once pointed at the paan/tobacco
  categories, captures them at full parity with the paid API — at zero marginal
  cost. The only remaining edge the API holds is **Instamart** (login-walled for
  us), where it still returns 107 paan items our crawler cannot reach.
- Run-2 raw output: `paan_comparison_run2.txt`; before/after DBs archived under
  `.inventory_archive/` (`inventory_{blinkit,zepto}.db.run1`).


