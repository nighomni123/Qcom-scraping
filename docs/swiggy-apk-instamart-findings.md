# Swiggy APK — Instamart Reverse-Engineering Findings

**APK:** `in.swiggy.android_4.115.1-1817_4arch_3dpi...apkm` (Swiggy consumer app)
**Focus:** Instamart tabs / sections, API surface, anti-bot / signing
**Method:** `apktool d` (smali + decoded manifest) + raw DEX string extraction + targeted smali grep (`/Users/Mitesh Gada/revtools`, persistent toolchain). No source-level decompile needed for these findings.
**Companion:** Zepto findings in `docs/zepto-apk-reverse-findings.md` (archived alongside this report).

---

## 1. Headline conclusions (crawler-relevant)

- **Swiggy's app-side anti-bot is AWS WAF — same family as Zepto.** `com.amazonaws.waf.mobilesdk.c.*` classes are present, and a network interceptor reads `x-aws-waf-token`, `x-amzn-waf-action`, `x-amzn-waf-rate-limit`. The WAF token is the hard gate; it is delivered as `x-aws-waf-token` (header) / `aws-waf-token` (cookie).
- **Native API auth uses an HMAC scheme (`x-swiggy-auth`).** `network/interceptors/b.smali` sets `x-swiggy-auth`; HMAC machinery (`populateHMAC`, `validateHMAC`, `CalculateMac`, `calculateMac`, `calculateX2s`) is in the DEX. Plus `x-oztok`, `x-channel`, and device headers (`x-device-network`/`x-device-memory`/`x-device-frame`).
- **Instamart is a `webviewV2` + `externalWidget` "instamart" surface**, not a fully native screen. Its WebView shell bundle is `im_shell` = `https://media-assets.swiggy.com/assets-aggregator/im_main_v3.json`.
- **Its tabs/sections are server-driven gandalf/widgets/v2 protobufs** (Square Wire) served by the **Discovery** service at `disc.swiggy.com` (`evaluatePage` → `EvaluatePageResponse`), cached via `CacheEvaluatePageConfig`.
- **Conclusion for the crawler is identical to Zepto:** drive a real browser and harvest the **already-tokened / already-signed** responses. **Do NOT forge** `x-aws-waf-token` or `x-swiggy-auth`. The only hard gate we hit is IP-reputation — the `www.swiggy.com/instamart` SPA is login-walled for our exit IP, so the crawl target stays **`instamart.in`** (separate storefront, no login gate).

---

## 2. Instamart as a surface — entry & tabs

- Launched via deeplink **`swiggy://stores/instamart/loader`**.
- Declared in the app's widget config as:
  - `["webviewV2", "instamart"]`
  - `["externalWidget", "instamart"]`
- WebView shell config key `im_shell` → `https://media-assets.swiggy.com/assets-aggregator/im_main_v3.json` (defines the tab bar + `instamart_shell_allowed_deeplinks`).
- Availability is feature-flagged / runtime-gated:
  - `INSTAMART_L2` (a level-2 Instamart experience flag)
  - `instamartAvailable=` / `instamaxAvailable=` in `BottomBarInfo` (bottom-nav tab availability)
  - `include_instamart_pathway_loader`, `launch_discovery_wrapper_fragment`
- It is a distinct **business line** — `com/swiggy/platform/shared/marketplace/v1/BusinessLine` is threaded through gandalf widgets.

---

## 3. Instamart sections (deeplinks)

| Section | Deeplink / route | Native API |
|---|---|---|
| Loader / entry | `swiggy://stores/instamart/loader` | — |
| Search | `swiggy://stores/instamart/search` (`/instamart/search?custom_back=true`) | `/api/v1/instamart/presearch`, `/api/v1/instamart/search-config` |
| Category listing (with filters) | `instamart/category_listing_filter` | — |
| Wishlist | `swiggy://stores/instamart/wishlist` | — |
| MxN curated collections | `swiggy://stores/instamart/campaign-collection/mxn?...&layoutId=<id>&customerPage=STORES_MxN_*` | evaluate-page by `layoutId` |
| Product detail (PDP) | `www.swiggy.com/stores/instamart/p/` | `/api/v1/instamart/product`, `/api/v1/instamart/seo-product` |
| Item pass (subscription) | `instamart/item-pass-selection-page` | `/api/v1/instamart/pass/activate` |
| Smart / Personalised Discovery | `instamart/personalised_discovery` | `/api/v1/instamart/contextual_discovery` |
| Cart | (Lynx-rendered) | `/api/v2/view?cartType=INSTAMART`, `/api/v2/view/INSTAMART?pageType=INSTAMART_CART` |

**MxN campaign collections** are curated matrix/grid layouts — concrete examples found in assets:
- **Gourmet** → `layoutId=12276`, `customerPage=STORES_MxN_52`
- **Handpicked** → `layoutId=5984`, `customerPage=STORES_MxN_3`, `launch_discovery_wrapper_fragment=true`

These are worth probing directly on `instamart.in` (same `layoutId`/`customerPage` params) as curated category grids.

---

## 4. The "sections" rendering model — gandalf/widgets/v2

- Instamart home & category pages are **"Evaluate Pages."** The Discovery service (`disc.swiggy.com`) exposes `evaluatePage`; the response type is `com.swiggy.instamartgateway.discovery.v1.EvaluatePageResponse` (a Square Wire protobuf adapter).
- It wraps `com.swiggy.gandalf.widgets.v2.SuccessResponse` / `FailureResponse`. The **widget tree IS the section layout**. Widget types referenced (from `swiggy/gandalf/widgets/v2/*.proto`): `Tab`, `Tabs`, `Card`, `CardList`, `GroupedCard`, `Cta`, `Banner`, `BottomBar`, `Icon`, `Meta`, `Copy`, `Tag`, `Tags`, `Plan`, `Faq`, `Layout`, `ImageInfoLayoutCard`, `LotteInfoLayoutCard`, `FloatingProgressButton`, `TooltipV2`, `Analytics`, `FrequencyCapping`, etc.
- **Caching:** `CacheEvaluatePageConfig` and `CacheEvaluatePageWPConfig(ttl_in_seconds=...)` — evaluate pages (home/sections) are precomputed and cached with a TTL. `InstamartDiscoveryCacheManager` + `InstamartDiscoveryStoreManager` manage per-store discovery caching.
- **Smart Discovery:** `instamart/personalised_discovery`, `UpdateSmartDiscoveryFacetsVM`, gated by `launch_discovery_wrapper_fragment` — a personalized section.
- The Discovery call carries header **`X-Discovery-Context`**.

> Note: the gandalf widget *schema* lives in the web bundle / proto defs, not the APK's signing logic. The APK just deserializes the protobuf and bridges it to the WebView/native widgets.

---

## 5. Native Instamart API surface (host = Swiggy origin)

### 5a. Legacy path `/api/v1/instamart{...}` (host `www.swiggy.com`)
- `/api/v1/instamart/{pageID}` — evaluate page by id (a section page)
- `/api/v1/instamart/product` — product
- `/api/v1/instamart/seo-product` — SEO product
- `/api/v1/instamart/presearch` — search prefetch
- `/api/v1/instamart/search-config` — search config
- `/api/v1/instamart/contextual_discovery` — contextual discovery
- `/api/v1/instamart/user-preferences/update` — preferences
- `/api/v1/instamart/complimentary_item` — freebie
- `/api/v1/instamart/relay-vendor-order` — vendor order relay
- `/api/v1/instamart/pass/activate` — item pass activation

### 5b. Unified BFF `/api/v2/view?cartType=INSTAMART` (Retrofit `IInstamartApi`, `in/swiggy/android/tejas/im/feature/IInstamartApi`)
- `GET /api/v2/view?cartType=INSTAMART` — get cart (`getInstamartCart`)
- `POST /api/v2/view/INSTAMART?pageType=INSTAMART_HOME` — home layout
- `POST /api/v2/view/INSTAMART?pageType=INSTAMART_CART` — cart layout (improvement)
- `POST /api/v2/view/clear-cart?cartType=INSTAMART` — clear cart
- Methods: `getInstamartCart`, `clearInstamartCart`, `updateCart`, `getInstamartCartWithLayoutDetails`

### 5c. Cart UI
- The cart is rendered via **Lynx** (Swiggy's cross-platform engine): `swiggylynx/.../instamart` plugin.

### 5d. Location seeding (directly relevant to the crawler)
- `InstamartLatLngQueryInterceptor` (`in/swiggy/android/network/interceptors/`) intercepts any request whose path starts with **`/api/v1/instamart`** and injects lat/lng as query params.
- Feature flag **`enable_im_lat_long_full_names`** selects the param names:
  - `LatLngMode.SHORT` → `lat` / `lng`
  - `LatLngMode.FULL` → `latitude` / `longitude`
- **Takeaway:** the Instamart API expects `lat`/`lng` (default SHORT) or `latitude`/`longitude` as query params. This matches the crawler's existing location rewrite (localStorage + cookie + request-path/query rewrite). Keep using `lat`/`lng` short form.

---

## 6. Anti-bot / request signing (app side)

| Mechanism | Evidence | Role |
|---|---|---|
| **AWS WAF** | `com.amazonaws.waf.mobilesdk.c.*`; interceptor reads `x-aws-waf-token`, `x-amzn-waf-action`, `x-amzn-waf-rate-limit` | Hard gate. Token delivered as `x-aws-waf-token` header / `aws-waf-token` cookie. **Same as Zepto.** |
| **HMAC API auth** | `network/interceptors/b.smali` sets `x-swiggy-auth`; DEX has `populateHMAC`, `validateHMAC`, `CalculateMac`, `calculateMac`, `calculateX2s` | Signs native API requests (`x-swiggy-auth`). `x-clear-swiggy-auth` drops it on logout. |
| **Token / channel headers** | `x-oztok`, `x-channel`, `x-device-network`, `x-device-memory`, `x-device-frame`, `x-cache` | Per-request context/telemetry. |
| **Discovery context** | `X-Discovery-Context` | Scopes the `disc.swiggy.com` evaluate-page call. |
| **Device fingerprint (fraud)** | `com.bureau.devicefingerprint` SDK; Android `Signature` collection | Risk/fraud, not API signing. |

**Crawler verdict:** identical to Zepto. The browser/app obtains `x-aws-waf-token` and computes `x-swiggy-auth` itself. Our in-browser crawl harvests already-tokened/already-signed responses — **do NOT attempt to forge the WAF token or the HMAC signature.**

---

## 7. Hosts observed (from DEX strings)

| Host | Use |
|---|---|
| `https://www.swiggy.com/instamart` / `…/instamart/` | App's Instamart SPA (login-walled for our exit IP) |
| `https://instamart.in` | **Separate storefront — our crawl target** (no login gate) |
| `https://disc.swiggy.com` | Discovery service (`evaluatePage`, gandalf widgets) |
| `https://fulfillment-middleware.swiggy.com` | Fulfillment middleware |
| `https://media-assets.swiggy.com/assets-aggregator/im_main_v3.json` | Instamart WebView shell bundle |
| `https://instamart-media-assets.swiggy.com` | Instamart image assets |
| `https://stores.swiggy.com/connect`, `…/health-check` | Store service |
| `https://webviews.swiggy.com`, `https://chkout.swiggy.com`, `https://payments.swiggy.com` | Webviews / checkout / payments |

---

## 8. Crawler notes & live verification (`tools/pw_catalog.js`, instamart adapter)

1. **Keep `instamart.in` as the target.** The app's `www.swiggy.com/instamart` is login-walled for our IP; `instamart.in` is a separate storefront that serves a default catalog.
2. **No signature/WAF forging.** Rely on the browser's own `x-aws-waf-token` + signed `x-swiggy-auth`. Harvest already-signed responses.
3. **Location seeding:** continue injecting `lat`/`lng` (SHORT mode) / `latitude`/`longitude` (FULL) — matches `InstamartLatLngQueryInterceptor`.
4. **Deep/collection URLs need a store-bound warm-up (fixed).** `instamart.in` collection/deep pages (e.g. `/campaign-collection/mxn`) do NOT expose the "Add your location" trigger, so the crawler cannot open the address modal there and they stay store-less (no product fetch). `pw_catalog.js` now binds a store on the **homepage first**, then loads the target URL so the store context (cookies) carries over. This also fixed a silent 0-product bug where the wait-loop short-circuited on the warm-up homepage's own catalog before the target page loaded.
5. **Response body cap raised 3 MB → 12 MB (fixed).** Instamart's store-gated `home/v2` catalogs routinely exceed 3 MB and were being dropped before parsing, so whole catalog pages returned 0.
6. **MxN campaign collections are widget-layout shells, NOT a product list (verified live).** `campaign-collection/mxn?layoutId=12276&customerPage=STORES_MxN_52` (Gourmet) and `…layoutId=5984…` (Handpicked) load fine, bind a store, and fire `home/v2?layoutId=4987&storeId=…` (HTTP 200) — but that payload is only **~87 KB** with `top keys: ['data','statusCode']` and **zero** `name`/`price`/`sellingPrice`/`mrp` fields (just ~50 section `title`s). It is a **gandalf/widget layout shell** (section/banner/widget config), not inline products. The ~56 products seen on these runs actually came from the **warm-up homepage's** `home/v2` (which inlines products), not the collection. **Conclusion: MxN collections do not add a distinct, harvestable product set on the web — products come from the standard homepage catalog already captured. Do NOT add these URLs to the crawl loop (would add ~90 s latency per URL for ~0 net new products).** If we later want curated grids, the work is to parse the gandalf/widget tree (the same framework `disc.swiggy.com` `evaluatePage` serves as protobuf), not to hit these URLs blindly.
7. **Correct collection labeling.** Any instamart target whose URL matches `campaign-collection/mxn?…layoutId=<id>` is now labeled `mxn:<id>` (everything else `home`); warm-up homepage products are dropped so only the target page's products remain attributed.
8. **Discovery endpoint** `disc.swiggy.com` `evaluatePage` is the source of the home/section widget tree (same gandalf framework) — only relevant if we drive the app WebView; not needed for `instamart.in`.
