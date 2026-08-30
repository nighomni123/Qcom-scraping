# Zepto Consumer APK — Reverse Engineering Findings
**Artifact:** `apks/com.zeptoconsumerapp_26.8.6-742_4arch_7dpi_82a254ba9472cb6fdabd8d79ee8855b3_apkmirror.com.apkm`
**Version:** com.zeptoconsumerapp 26.8.6 (build 742)
**Tooling used:** `jadx 1.5.6` (+ portable Temurin JRE 17) for DEX→Java; `apktool 3.0.3` for smali + decoded manifest/resources. Both downloaded and bootstrapped locally (no system Java was present).

## 1. App architecture (high level)
- **React Native + Hermes** app. Primary DEX (`classes.dex` family, 5 files, ~28 MB) is the RN/Metro JS bundle `assets/index.android.bundle` (16 MB, minified JS, not Hermes bytecode).
- The **shopping experience runs inside a WebView** that loads `https://www.zeptonow.com` / `https://shop.zeptonow.com`. The native layer *injects* privileged headers and a WAF token into that WebView.
- Anti-bot / request gating is **AWS WAF** (see §3), not a custom signature scheme in the APK.

## 2. Key assets discovered
- `assets/api_key.txt` — **NOT** the Zepto BFF key. It is an **Amazon Login-with-Amazon (LWA) OAuth API key JWT** (`clientId amzn1.application-oauth-client…`, issuer Amazon, type `APIKey`, pkg `com.zeptoconsumerapp`). A distractor for API-signing research; relevant only to Amazon Pay.
- `assets/version.properties` — **Juspay** payments SDK message-signing *salt template* (base64-ordered field scheme for payment message hashing). Confirms Juspay/HyperSDK is the payments stack.
- `assets/com/amazonaws/waf/mobilesdk/d/96ed8a0b…` and `f9dca457…` — AWS WAF Mobile SDK's bundled (obfuscated) challenge assets.

## 3. Anti-bot / WAF = AWS WAF (Mobile SDK + JS SDK)
- Bundled SDK: **`com.amazonaws.waf.mobilesdk`** (`WAFConfiguration`, `WAFTokenProvider`, `WAFToken`, `WAFTokenResultCallback`, `ApplicationIntegrationURL`, `DomainName`).
- Native module: **`com.zeptoconsumerapp.WafIntegration.WafModule`** (RN name `"NativeWaf"`, extends `NativeWafSpec`). It wraps `WAFTokenProvider`.
- Initialization (decompiled `WafModule.initialize`):
  ```java
  new WAFTokenProvider(ctx, WAFConfiguration.builder()
      .applicationIntegrationURL(applicationIntegrationUrl)   // passed from JS
      .domainName(domainName)                                 // passed from JS
      .backgroundRefreshEnabled(true)
      .maxErrorTokenRefreshDelayMSec(1000L)
      .setTokenCookie(true)                                  // token sent as a COOKIE
      .build());
  ```
  - The two config values (`applicationIntegrationUrl`, `domainName`) are **not hardcoded in the bundle** — they are passed from JS (likely from remote config). `domainName` is the protected host (e.g. `zeptonow.com`); `applicationIntegrationUrl` is the AWS WAF JS-integration endpoint.
  - On token ready, `WafModule` emits a `WafTokenReady` RN event carrying `WAFToken.getValue()`.
- Within the WebView, the injected script calls **`AwsWafIntegration.getToken()`** (AWS WAF **JS** SDK) and posts `{type:'waf-token', token, timeTakenMs, apiCallTimeMs}` back to RN. Retry loop: `maxTokenAttempts=3`, `tokenDeadlineMs=30000`, with `acquireTokenWithTimeout()` racing a timeout promise.
- **Token delivery:** `setTokenCookie(true)` ⇒ the AWS WAF token is carried as the **`aws-waf-token`** cookie/header. Confirmed by bundle field `addressTextBox-aws-waf-token` and `x-aws` references. This is the hard gate AGENTS.md calls the "IP-reputation 403 wall".

## 4. Request headers observed (native layer + WebView bridge)
- `X-XSRF-TOKEN` (web/app CSRF header)
- `X-Client-IP`
- `waf-token` (postMessage type from WebView → RN)
- `aws-waf-token` (the AWS WAF token, cookie/header)
- The **API request-signing** (`request-signature`, `x-csrf-secret`, `x-xsrf-token`, `x-api-key`, `x-timezone`) is **NOT in the APK** — it lives in the **web** app's JS (per AGENTS.md: bundle chunks `89411-*.js` / `88682-*.js`, the `XMLHttpRequest.open` wrapper). The APK only supplies the WAF token + device/session/store ids to the WebView.

## 5. API hosts / endpoints (from bundle + manifest)
- App (native/fetch) gateway: **`https://api-gateway.zepto.co.in`** — microservices mesh:
  `oms`, `payment-service`, `rewards-service`, `zerox-service`, `recipe-service`, `serviceability-service`, `shop-agent-service`, `wallet-service`, `coupon-service`, `exp-svc`, `invoicing-service`, `partner-integration-service`, `chat-service`.
- Also: `https://api.zepto.co.in/api`, `https://events.zepto.co.in` (analytics), `https://lite.zeptonow.com`, `https://devspace.zeptonow.com/api` (dev), `https://api-gateway.zeptonow.dev/*` (dev).
- WebView: `https://www.zeptonow.com`, `https://shop.zeptonow.com`, `https://bff-gateway.zepto.com` (web BFF, per AGENTS.md).
- CDNs: `https://ik.imagekit.io/jupdt2k6txi/...` (images), `https://d1lkfeqm0367ds.cloudfront.net/...` (config/service URLs, e.g. `qa-base-address-service-url`).
- Voice TTS: `https://api-india.cartesia.ai/tts/...`.
- Wallet service uses **Kong** (`WALLET_KONG_SERVICE_BASE_URL`).

## 6. Native secret store
- `com.zeptoconsumerapp.nativelocalstorage.SecureKeysNative` loads `libsecurekeys.so` and exposes `static native String getKey(String)`. Wrapper `NativeLocalStorageModule.getSecureKey(str)` → `SecureKeysNative.getKey(str)`. Key **names** are passed from JS at runtime (not present as literals in the bundle), so the exact secret names could not be enumerated statically. `libsecurekeys.so` (4.4 KB) is the obfuscated key vault; `libappmodules.so` (3.3 MB) hosts the `NativeWaf` + `NativeEncryptedMMKV` TurboModule JSI specs.

## 7. Manifest / security posture
- 28 permissions incl. `READ_SMS`/`RECEIVE_SMS`/`SEND_SMS` (OTP auto-read), `READ_CONTACTS`, `CAMERA`, `NFC`, `RECORD_AUDIO`, `DETECT_SCREEN_CAPTURE` (anti-screenshot), `GET_ACCOUNTS`.
- **No exported components** declared; single RN activity.
- **Network security config**: Zepto's own domains are **HTTPS-only (no cleartext, no cert pinning on Zepto hosts)**. Cleartext is permitted *only* for OTP/carrier partners: `*.safr.sekuramobile.com`, `partnerapi.jio.com`, `in-vil.ipification.com`, `api-csp.airtel.in`. ⇒ Our Playwright interception (browser-level capture, not MITM) is unaffected.

## 8. Implications for the Moneymaker crawler (pw_catalog.js)
- The web-signing (`request-signature` etc.) already documented in AGENTS.md remains authoritative; the APK does not override it.
- The **AWS WAF token** is the real gate for `bff-gateway.zepto.com`. In a real browser (Playwright), the AWS WAF JS SDK runs naturally and yields the `aws-waf-token`, so the current "harvest already-signed responses" approach is correct — **do not try to forge the WAF token** (it's a solved challenge bound to the session). The only hard gate remains IP reputation (403), as AGENTS.md notes.
- `api_key.txt` (Amazon LWA) is irrelevant to crawling Zepto; don't confuse it with an `x-api-key`.

## 9. Artifacts produced (in /tmp/zepto_re — EPHEMERAL, wiped on reboot)
- `/tmp/zepto_re/decompiled/` — jadx Java sources (28,851 files; ~12.5k had minor decompile errors, targets intact).
- `/tmp/zepto_re/smali/` — apktool smali + decoded `AndroidManifest.xml` + `res/xml/network_security_config.xml`.
- `/tmp/zepto_re/x/assets/index.android.bundle` — extracted RN bundle (string/context analysis done).
- Key decompiled classes: `WafIntegration/WafModule.java`, `WafIntegration/WafPackage.java`, `NativeWafSpec.java`, `nativelocalstorage/SecureKeysNative.java`, `NativeLocalStorageModule.java`.
