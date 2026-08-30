# Blinkit (Grofers) APK — Reverse-Engineering Findings

**APK:** `com.grofers.customerapp_18.23.0-280180230...blinkit.apkm` (legacy **Grofers** package name — Blinkit was Grofers; the app is now **Zomato-owned**). `base.apk` 87 MB, 13 DEX files.
**Method:** `apktool d` (smali + decoded manifest) + DEX string table + targeted smali grep (persistent toolchain at `/Users/Mitesh Gada/revtools`). Companion reports: `docs/zepto-apk-reverse-findings.md`, `docs/swiggy-apk-instamart-findings.md`.

---

## 1. Headline conclusions (crawler-relevant)

- **No AWS WAF / no app-enforced WAF** — unlike Zepto and Swiggy (both sit behind `com.amazonaws.waf.mobilesdk` + `x-aws-waf-token`). Blinkit's only on-device anti-abuse is **Google Play Integrity** (device attestation), which does **not** apply to our browser crawl. So Blinkit is the *easiest* of the three QC apps on the anti-bot front.
- **API auth = a hardcoded client credential + API key + session token.** The DEX contains `Authorization: Basic base64("cde_external:uq8vGL99wd4RfP4ER33GxnU3")`. Combined with `X-Zomato-API-Key` (and a session token) this authenticates every API call. **This is an extractable embedded secret** — recoverable from the APK in seconds.
- **No per-request HMAC signature** (unlike Swiggy's `x-swiggy-auth` / Zepto's `request-signature`). Blinkit trusts Basic + API-key, so the API is *less signed* and simpler to drive. Our in-browser crawl harvests already-authenticated responses — **no forging needed, and no WAF token to solve**.
- **Sections/tabs are a layout-engine.** Home/feed sections come from `/v1/layout/feed` (and `/v2/layout/feed`); per-screen layouts from `/v1/layout/{screen}` (`product`, `search`, `cart_preview_modified`, `shared_address_details`, `order_*`, …). Static widget assets from `cdn.grofers.com/layout-engine/v2/`. This is Blinkit's analog of Swiggy's gandalf `evaluatePage`.
- **Zomato-owned:** shares `com.zomato.*` infra (`locationkit`, `walletkit`, `paymentkit`, `commons`, `ui/lib`). Location = Zomato `locationkit` lat/lng (same lat/lng rewrite as the other apps).

---

## 2. Anti-bot / request signing (app side)

| Mechanism | Evidence | Role |
|---|---|---|
| **Google Play Integrity** | `requestExpressIntegrityToken`, `INTEGRITY_TOKEN_PROVIDER_INVALID`, `playcore.integrity.*` | App-side device attestation. Does NOT gate the web/API at the network layer; browser crawl unaffected. |
| **Basic auth (embedded)** | `Authorization: Basic Y2RlX2V4dGVybmFsOnVxOHZHTDk5d2Q0UmZQNEVSMzNHeG5VMw==` → decodes to `cde_external:uq8vGL99wd4RfP4ER33GxnU3` | Static API-gateway client credential (`ApiGW(clientApiKey=...)`). **Embedded secret.** |
| **API key** | `X-Zomato-API-Key`, `x-api-key`, `x-goog-api-key` | Per-request API-key header. Blinkit's own key header is `X-Zomato-API-Key` (Zomato infra). |
| **Session token** | `DIGEST_AUTHORIZATION_HEADER_FORMAT`, `BASIC_AUTHORIZATION_HEADER_FORMAT`, session cookies | Authenticated session after login. |
| **CDN/LB request IDs** | `akamaiRequestId=`, `cloudflareTraceId=` | Incidental Akamai/Cloudflare *response* headers on static-asset delivery. **Not** an enforced WAF — no `cf-ray`/`cf_clearance`/`x-aws-waf-token` in front of the API. |
| **AWS SDK** | `com.amazonaws.mobileconnectors.remoteconfiguration` | Remote-config SDK only — **NOT** the `waf.mobilesdk` WAF SDK. So no AWS WAF. |
| **HMAC / signature** | *absent* — no `populateHMAC`, `CalculateMac`, `x-request-signature`, `x-sign` | Blinkit does **not** HMAC-sign requests. |

**Crawler verdict:** easier than Zepto/Swiggy. No WAF token to solve, no request signature to forge. The browser/app supplies `Authorization: Basic` + `X-Zomato-API-Key` + session automatically; we harvest the already-authenticated responses. (If anyone ever drives the raw API directly, the `cde_external` Basic credential + `X-Zomato-API-Key` are sufficient — but the browser approach already covers this, so do **not** hardcode the credential into the crawler.)

---

## 3. API surface

| Surface | Detail |
|---|---|
| **Prod API host** | `https://api3.blinkit.com` (config: `/config/domains`) |
| **Legacy / other hosts** | `https://api2.grofers.com`, `https://api.blinkit.dev` (dev) |
| **Section/layout endpoints** | `/v1/layout/feed`, `/v2/layout/feed` (home/feed sections = the "tabs"/widgets); `/v1/layout/product`, `/v1/layout/search`, `/v1/layout/cart_preview_modified`, `/v1/layout/shared_address_details`, `/v1/layout/order_*`, `/v1/layout/empty_search`, `/v1/layout/personalized_card`, … |
| **Cart / orders** | `/v3/cart/orders/{order_id}/cancel`, `/v2/sdk/configure`, `/v2/sdk/initiate_recharge_order`, `/v2/sdk/complete_recharge_order` |
| **Config** | `/v1/config/primary`, `/v1/CartDeliveryInstructionData` |
| **Asset CDN** | `cdn.grofers.com` (images + `layout-engine/v2/…` widget assets, `2024-…`/`2025-…` dated layouts, `blinkit-studio/…`) |
| **Product/listing data** | Fetched via separate listing endpoints referenced by the layout; the `/v1/layout/*` responses are the *section tree*, not the product rows. |

The **home "feed" is the section model**: `/v1/layout/feed` returns a widget/section tree (banners, carousels, category rows) — Blinkit's equivalent of Swiggy's gandalf `evaluatePage`. Curated "tabs"/sections are specific layout endpoints.

---

## 4. Location seeding

- Zomato `locationkit` (`com.zomato.android.locationkit`, `com.zomato.commons.ZLatLng`). Lat/lng are passed as query/path params (same pattern as the other QC apps). The crawler's existing `localStorage` + cookie + request-rewrite lat/lng seeding applies unchanged.

---

## 5. Embedded secrets (real & distractors)

- **Real embedded API credential:** `cde_external:uq8vGL99wd4RfP4ER33GxnU3` (Basic auth, API-gateway client). Extractable from the DEX. Not needed by the crawler (browser supplies it).
- `api_key.txt` referenced in assets — **likely a distractor file** (Zepto had the same pattern with an Amazon LWA key); verify before trusting.
- `amazon_pay_api_key`, `webApiKey`, `webPreprodApiKey`, `ApiGW(clientApiKey=…)`, `AmazonAPIKey` — assorted SDK keys (Amazon Pay, web client). Not the QC API auth.
- AWS remote-config SDK present (not WAF).

---

## 6. Hosts observed (DEX strings)

| Host | Use |
|---|---|
| `https://api3.blinkit.com` | **Prod API** |
| `https://api2.grofers.com` | Legacy Grofers API |
| `https://api.blinkit.dev` | Dev API |
| `https://cdn.grofers.com` | Asset + `layout-engine/v2` widget assets |
| Google / Firebase / Amazon Pay SDK endpoints | Third-party |

---

## 7. Recommendations for the crawler (`tools/pw_catalog.js`, blinkit adapter)

1. **Blinkit is the lowest-friction target** of the three QC apps — no WAF, no request signature. The existing blinkit adapter should keep working; no anti-bot workaround needed.
2. **No forging.** Harvest already-authenticated responses. Do **not** hardcode the embedded `cde_external` Basic credential into the crawler.
3. **Sections come from `/v1/layout/feed`** (gandalf-like widget tree). Only relevant if we drive the app WebView; the browser crawl harvests the rendered products regardless of the layout endpoint.
4. **Location seeding** via lat/lng as for the other apps.
5. **If a raw-API path is ever needed:** `Authorization: Basic base64("cde_external:uq8vGL99wd4RfP4ER33GxnU3")` + `X-Zomato-API-Key` + session token is the full auth set — but prefer the browser-intercept approach already in use.
