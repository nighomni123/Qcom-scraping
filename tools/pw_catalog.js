#!/usr/bin/env node
/**
 * pw_catalog.js — browser-intercept crawler for catalogs AND search pages.
 *
 * Strategy, in order:
 *   1. JSON interception: the app's own signed API responses are parsed
 *      generically (works fully on Blinkit).
 *   2. __NEXT_DATA__ / embedded JSON script tags.
 *   3. DOM extraction: site-specific CSS selectors (works on Amazon/Flipkart
 *      search results which render server-side HTML).
 *
 * Usage:
 *   PLAYWRIGHT_BROWSERS_PATH=... NODE_PATH=... node pw_catalog.js \
 *     --app blinkit --url https://blinkit.com/s/?q=amul+milk \
 *     --lat 19.119 --lon 72.846 [--wait-ms 12000] [--dump]
 */
const fs = require('fs');
let chromium;
try { ({ chromium } = require('playwright')); }
catch (e) { console.log(JSON.stringify({ error: 'playwright-not-importable: ' + e.message, products: [] })); process.exit(0); }

// Optional raw-body capture for field-name discovery (the app is the API docs):
//   DSH_BODY_DIR=/tmp/bodies node pw_catalog.js ...
const BODY_DIR = process.env.DSH_BODY_DIR || null;
let bodyN = 0;
function dumpBody(url, text) {
  if (!BODY_DIR) return;
  try {
    const safe = String(url).replace(/[^a-z0-9]+/gi, '_').slice(0, 70);
    fs.mkdirSync(BODY_DIR, { recursive: true });
    fs.writeFileSync(`${BODY_DIR}/${String(++bodyN).padStart(3, '0')}__${safe}.json`, text);
  } catch (_) {}
}

// Optional stuck-session diagnostics (e.g. Instamart's onboarding gate): when
// a run sits at zero products after the onboarding attempt, dump screenshot +
// clickable elements + API status log so the gate can be traced, not guessed:
//   DSH_DEBUG_DIR=/tmp/imdebug node pw_catalog.js ...
const DEBUG_DIR = process.env.DSH_DEBUG_DIR || null;

function arg(name, def) {
  const i = process.argv.indexOf('--' + name);
  return i > -1 ? process.argv[i + 1] : def;
}
const APP = arg('app', 'blinkit');
const URL_ = arg('url', 'https://blinkit.com/');
const LAT = parseFloat(arg('lat', '19.119'));
const LON = parseFloat(arg('lon', '72.846'));
const WAIT = parseInt(arg('wait-ms', '9000'), 10);
const DUMP = process.argv.includes('--dump');
// Deep-sweep options (Demand Radar watchlist builder): after the initial
// harvest, visit up to N category links found in the DOM, then run staple
// search terms — ALL inside this one browser session, so every intercepted
// catalog call stays signed/natural and costs one chromium launch total.
const CATEGORIES = parseInt(arg('categories', '0'), 10) || 0;
const TERMS = (arg('terms', '') || '').split('|').map(s => s.trim()).filter(Boolean);
// Category-label skips (config demand.skip_categories): QC apps order their
// category rail dairy/bread/eggs-first, so "first N links" over-samples milk.
// Pipe-separated, case-insensitive substrings; a skipped label frees its
// queue slot for the NEXT category — diversification at zero extra cost.
const SKIP = (arg('skip', '') || '').split('|').map(s => s.trim().toLowerCase()).filter(Boolean);
const CAT_WAIT = parseInt(arg('cat-wait-ms', '6500'), 10);

// Label attached to everything intercepted while visiting the current page.
let CURRENT_LABEL = 'home';

// Search URL shapes per app (stable UI routes — mirrors src/adapters/*.py).
// Zepto (08-22): migrated to www.zepto.com and the search route reads
// ?query= — ?q= loads a home shell and NEVER fires the search API
// (user-search-service/api/v3/search). Verified live.
const SEARCH_URLS = {
  'blinkit':   t => `https://blinkit.com/s/?q=${encodeURIComponent(t)}`,
  'zepto':     t => `https://www.zepto.com/search?query=${encodeURIComponent(t)}`,
  'instamart': t => `https://instamart.in/search?query=${encodeURIComponent(t)}`,
};

// Zepto prices arrive in paise (sellingPrice=1600 => ₹16); Blinkit/Instamart
// use rupees. Normalized in collect() so downstream stays rupee-only.
const PRICE_DIVISORS = { zepto: 100 };

// Desktop UAs. We deliberately do NOT use mobile emulation: an A/B on
// 2025-08-30 showed Zepto/Blinkit return byte-identical product JSON under a
// desktop context (same SKUs, store, ETA, field names) and Instamart's 403
// login wall is IP/anti-fraud based and unaffected by device profile. A
// desktop viewport also renders more carousel items per page. See repo thread.
const UAS = [
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
  'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
];

const num = s => {
  if (s === null || s === undefined) return null;
  if (typeof s === 'number') return s;
  const m = String(s).replace(/,/g, '').match(/(\d+(?:\.\d+)?)/);
  return m ? Number(m[1]) : null;
};

// ---- stock / scarcity normalization (Demand Radar phase 0) --------------
// Field names rotate per app/build — capture generically, normalize here.
// Verified live (Blinkit web feed, Aug 2025): inventory=<units>,
// is_sold_out=bool, product_state; Zepto/Instamart use available-family keys.
const STOCK_KEYS = ['in_stock', 'instock', 'is_available', 'available', 'availability',
                    'out_of_stock', 'outofstock', 'oos', 'stock', 'sold_out',
                    'stock_status', 'inventory', 'is_sold_out', 'product_state',
                    'isavail', 'issoldout'];
const STOCK_INVERT = new Set(['out_of_stock', 'outofstock', 'oos', 'sold_out', 'is_sold_out']);
const BADGE_KEYS = ['only_few_left', 'few_left', 'fast_selling', 'selling_fast', 'low_stock'];

function normBool(v) {
  if (typeof v === 'boolean') return v;
  if (typeof v === 'number') return v !== 0;
  if (typeof v === 'string') {
    const s = v.trim().toLowerCase();
    if (['true', 'yes', 'y', 'in_stock', 'instock', 'available', 'in', '1'].includes(s)) return true;
    if (['false', 'no', 'n', 'out_of_stock', 'oos', 'unavailable', 'sold_out', 'out', '0'].includes(s)) return false;
  }
  return null;
}

/** stock-ish key/value -> true|false|null (null = unknown; never invent). */
function stockState(key, v) {
  if (v && typeof v === 'object' && !Array.isArray(v)) {
    v = v.status ?? v.in_stock ?? v.inStock ?? v.value ?? v.count ?? v.availability;
    if (v === null || v === undefined || typeof v === 'object') return null;
  }
  const b = normBool(v);
  if (b === null) return null;
  return STOCK_INVERT.has(String(key).toLowerCase()) ? !b : b;
}

// ---- response-level metadata: darkstore identity + delivery ETA ----------
const STORE_KEY_PRIORITY = ['store_id', 'storeid', 'dark_store_id', 'darkstoreid',
                            'merchant_id', 'merchantid',
                            'warehouse_id', 'warehouseid', 'wh_id', 'whid',
                            'vendor_id', 'vendorid', 'dc_id', 'store', 'warehouse',
                            'pod_id', 'podid'];
const ETA_KEY_RE = /(^|_)(eta|eta_minutes|eta_mins|delivery_time|deliverytime|delivery_eta|sla)(_|$)/i;

const META = { stores: new Map(), etas: [] };

function looksLikeId(v) {
  if (typeof v === 'number') return Number.isFinite(v) && v > 0 && Number.isInteger(v);
  if (typeof v !== 'string') return false;
  const s = v.trim();
  if (!s.length || s.length > 64) return false;
  if (/^(true|false|null|none|undefined)$/i.test(s)) return false;
  if (/^\d+\.\d+$/.test(s)) return false;          // coordinate-like float
  return !/[\/\\\s]/.test(s);
}

function addStoreCandidate(key, value, label) {
  if (!looksLikeId(value)) return;
  const k = `${String(key).toLowerCase()}=${String(value).trim().toLowerCase()}`;
  const cur = META.stores.get(k) || { key: String(key).toLowerCase(), value: String(value).trim(), count: 0, label: null };
  cur.count += 1;
  if (!cur.label && typeof label === 'string' && label.length > 1) cur.label = label.slice(0, 80);
  META.stores.set(k, cur);
}

function addEtaCandidate(v) {
  let mins = null;
  if (typeof v === 'number') mins = v;
  else if (typeof v === 'string') { const m = v.match(/(\d{1,3})\s*(min|m\b|$)/i); if (m) mins = Number(m[1]); }
  else if (v && typeof v === 'object') {
    const raw = v.value ?? v.minutes ?? v.time ?? v.eta;
    if (typeof raw === 'number') mins = raw;
    else if (typeof raw === 'string') { const m = raw.match(/(\d{1,3})/); if (m) mins = Number(m[1]); }
  }
  if (mins !== null && Number.isFinite(mins) && mins >= 3 && mins <= 240) META.etas.push(mins);
}

function extractMeta(node, depth) {
  // depth 16: Instamart nests store widgets ~13 levels deep
  // (data.cards[].cardList.cards[].card.gridElements…items[].variations[]).
  if (!node || typeof node !== 'object' || depth > 16) return;
  if (Array.isArray(node)) { for (const v of node) extractMeta(v, depth + 1); return; }
  for (const [k, v] of Object.entries(node)) {
    const lk = String(k).toLowerCase();
    if (STORE_KEY_PRIORITY.includes(lk)) {
      if (v && typeof v === 'object' && !Array.isArray(v)) {
        addStoreCandidate(lk.endsWith('_id') ? lk : lk + '_id',
                          v.id ?? v.store_id ?? v.warehouse_id ?? v.vendor_id,
                          v.name ?? v.title);
      } else {
        addStoreCandidate(lk, v, null);
      }
    }
    if (ETA_KEY_RE.test(k)) addEtaCandidate(v);
    extractMeta(v, depth + 1);
  }
}

// ---- generic product extraction from arbitrary catalog JSON ----
function collect(node, out, depth) {
  if (!node || typeof node !== 'object' || depth > 16) return;
  if (Array.isArray(node)) { for (const v of node) collect(v, out, depth + 1); return; }
  const keys = Object.keys(node);
  const lower = k => k.toLowerCase();
  // 'displayname' covers Instamart's camelCase displayName (items + variations).
  const nameKey = keys.find(k => ['name', 'title', 'product_name', 'display_name',
                                  'displayname', 'item_name'].includes(lower(k)));
  // Zepto variant nodes carry price/stock but nest the human name on the
  // parent product object — fall back to node.product.name.
  const hasProductFallback = !nameKey && node.product && typeof node.product === 'object';
  const priceKey = keys.find(k => ['selling_price', 'sellingprice', 'discounted_price',
                                   'discountedsellingprice', 'final_price', 'offer_price',
                                   'price'].includes(lower(k)));
  const mrpKey = keys.find(k => ['mrp', 'marked_price', 'original_price', 'list_price'].includes(lower(k)));
  if ((nameKey || hasProductFallback) && priceKey) {
    // Blinkit feed nests name/mrp as styled objects ({text:"₹75", ...}) — unwrap.
    let name = nameKey ? node[nameKey]
                       : (node.product.name ?? node.product.display_name ?? null);
    if (name && typeof name === 'object') name = name.text ?? name.display_name ?? null;
    let price = node[priceKey];
    if (price && typeof price === 'object') {
      // Blinkit {text|value}, Instamart google-money style
      // ({offerPrice:{units:"18"}}), or plain wrapper objects.
      price = price.value ?? price.amount ?? price.offerPrice ?? price.sellingPrice ??
              price.finalPrice ?? null;
      if (price && typeof price === 'object') {
        price = price.units ?? price.unitAmount ?? price.text ?? null;
      }
    }
    price = num(price);
    if (price !== null && PRICE_DIVISORS[APP]) price = price / PRICE_DIVISORS[APP];
    const mrpRaw = mrpKey ? node[mrpKey] : null;
    let mrp = num(mrpRaw && typeof mrpRaw === 'object'
                   ? (mrpRaw.text ?? mrpRaw.value ?? mrpRaw.units) : mrpRaw);
    if (mrp === null && node[priceKey] && typeof node[priceKey] === 'object') {
      // Instamart nests mrp inside the price object: price.mrp.{units}
      const m2 = node[priceKey].mrp;
      if (m2) mrp = num(typeof m2 === 'object' ? (m2.units ?? m2.value ?? m2.text) : m2);
    }
    if (mrp !== null && PRICE_DIVISORS[APP]) mrp = mrp / PRICE_DIVISORS[APP];
    if (name && typeof name === 'string' && Number.isFinite(price) && price > 0) {
      let stock = null;
      for (const k of keys) {
        const lk = k.toLowerCase();
        if (STOCK_KEYS.includes(lk)) {
          const s = stockState(lk, node[k]);
          if (s !== null) stock = s;
        }
      }
      const badges = keys.filter(k => BADGE_KEYS.includes(k.toLowerCase()) && node[k])
                         .map(k => k.toLowerCase());
      const id = node.id ?? node.sku ?? node.skuId ?? node.productId ?? node.product_id ?? name;
      const key = String(id).toLowerCase().replace(/\s+/g, '');
      const rec = {
        sku_key: key,
        name: String(name).slice(0, 140),
        price,
        mrp: Number.isFinite(mrp) && mrp > 0 ? mrp : null,
        url: node.url || node.link || node.seo_url || '',
        in_stock: stock,
        ...(badges.length ? { badges } : {}),
      };
      const prev = out.get(key);
      if (prev) {
        // Re-sighting (same SKU on home + category + search): refresh fields,
        // keep the freshest stock state, accumulate collection labels.
        prev.name = rec.name;
        prev.price = rec.price;
        if (rec.mrp) prev.mrp = rec.mrp;
        if (rec.url) prev.url = rec.url;
        if (rec.in_stock !== null) prev.in_stock = rec.in_stock;
        if (rec.badges) prev.badges = rec.badges;
        if (!prev.collections.includes(CURRENT_LABEL)) prev.collections.push(CURRENT_LABEL);
      } else {
        rec.collections = [CURRENT_LABEL];
        out.set(key, rec);
      }
    }
  }
  for (const v of Object.values(node)) collect(v, out, depth + 1);
}

// ---- DOM extractors for HTML-rendered search results ----
// NOTE: these run INSIDE the page via page.evaluate, so they must be fully
// self-contained — no references to Node-side helpers.
const DOM_EXTRACTORS = {
  'amazon.in': () => {
    const num = s => s === null || s === undefined ? null : (String(s).replace(/,/g, '').match(/(\d+(?:\.\d+)?)/) ? Number(String(s).replace(/,/g, '').match(/(\d+(?:\.\d+)?)/)[1]) : null);
    const rows = [];
    document.querySelectorAll('div[data-component-type="s-search-result"]').forEach(el => {
      const title = el.querySelector('h2 span')?.textContent?.trim();
      const price = num(el.querySelector('span.a-price span.a-offscreen')?.textContent);
      const mrpTxt = el.querySelector('span.a-price.a-text-price span.a-offscreen')?.textContent;
      const link = el.querySelector('h2 a')?.href || '';
      if (title && price) rows.push({ title, price, mrpTxt, link });
    });
    if (!rows.length) {
      document.querySelectorAll('[data-asin]:not([data-asin=""])').forEach(el => {
        const title = el.querySelector('h2 a span, h2 span')?.textContent?.trim();
        const price = num(el.querySelector('.a-price .a-offscreen')?.textContent);
        if (title && price) rows.push({ title, price, mrpTxt: null, link: '' });
      });
    }
    return rows;
  },
  'flipkart.com': () => {
    const num = s => s === null || s === undefined ? null : (String(s).replace(/,/g, '').match(/(\d+(?:\.\d+)?)/) ? Number(String(s).replace(/,/g, '').match(/(\d+(?:\.\d+)?)/)[1]) : null);
    const rows = [];
    document.querySelectorAll('a[data-id], div[data-id] a').forEach(el => {
      const title = el.querySelector('div.KzDlHX, div._4rR01T, div.s1Q9rs')?.textContent?.trim()
                 || el.querySelector('div[title]')?.getAttribute('title');
      const price = num(el.querySelector('div.Nx9bqj, div._30jeq3')?.textContent);
      const mrpTxt = el.querySelector('div.yRaY8j, div._3I9_wc')?.textContent;
      let href = el.getAttribute('href') || '';
      if (href.startsWith('/')) href = 'https://www.flipkart.com' + href;
      if (title && price) rows.push({ title, price, mrpTxt, link: href });
    });
    if (rows.length) return rows;
    // New mobile layout rotates class names — parse the visible text instead:
    // lines alternate  <title> / ₹<price> (sometimes with status lines between).
    const lines = document.body.innerText.split('\n').map(s => s.trim());
    for (let i = 0; i < lines.length; i++) {
      const m = lines[i].match(/^₹\s?([\d,]+)$/);
      if (!m) continue;
      let j = i - 1, title = null;
      while (j >= 0) {
        const t = lines[j];
        if (t && !/^₹/.test(t) && !/^(currently unavailable|add to compare|more|of \d+)/i.test(t) && t.length > 3) {
          title = t; break;
        }
        j--;
      }
      if (title) rows.push({ title, price: Number(m[1].replace(/,/g, '')), mrpTxt: null, link: '' });
    }
    return rows;
  },
  'www.zepto.com': () => {
    const num = s => s === null || s === undefined ? null : (String(s).replace(/,/g, '').match(/(\d+(?:\.\d+)?)/) ? Number(String(s).replace(/,/g, '').match(/(\d+(?:\.\d+)?)/)[1]) : null);
    const rows = [];
    document.querySelectorAll('a[href*="/pn/"], a[href*="/product/"]').forEach(el => {
      const title = el.querySelector('p, h3, h4')?.textContent?.trim();
      const price = num(el.querySelector('p[class*="price"], span')?.textContent);
      if (title && price) rows.push({ title, price, mrpTxt: null, link: el.href || '' });
    });
    return rows;
  },
};
// zeptonow.com now 301-redirects to zepto.com — same extractor for both hosts.
DOM_EXTRACTORS['www.zeptonow.com'] = DOM_EXTRACTORS['www.zepto.com'];

function domExtract(page) {
  const host = new URL(URL_).hostname.replace(/^www\./, '');
  const key = Object.keys(DOM_EXTRACTORS).find(h => host === h.replace(/^www\./, '') || host.endsWith(h));
  const fn = DOM_EXTRACTORS[key];
  if (!fn) return [];
  return page.evaluate(fn).catch(() => []);
}

// ---- gated-session bootstrap ---------------------------------------------
// Fresh Instamart sessions land behind an address/onboarding sheet: home AND
// search serve cards:[] until it completes, and the direct search API 403s
// pre-onboard (see AGENTS.md). Instead of guessing endpoints we drive the
// app's OWN flow: click its location CTAs (the geolocation permission is our
// real anchor coordinate), let the signed calls fire naturally, and only
// when the session is stuck at zero products. Generic "continue/confirm"
// texts are allowed for Instamart only to avoid dismissing unrelated sheets.
// NB: 'deliver(ing)? to' was REMOVED from strong — it matched the sheet's
// "We deliver to" label (not a CTA) and burned every click round on it.
const ONBOARD_STRONG = 'use (my )?(current )?location|detect( my)? location|current location|confirm location|select (this|my|a) (address|location)|set (my )?location|save (this )?address|confirm (this )?address|use this address|deliver here|proceed';
const ONBOARD_SOFT = '^continue$|^confirm$|^next$|^save$|^done$';
async function clickThroughOnboarding(page) {
  let clicks = 0;
  let lastLabel = null;
  for (let round = 0; round < 6 && clicks < 5; round++) {
    const did = await page.evaluate(([strongSrc, softSrc, allowSoft, lastLabel]) => {
      const strong = new RegExp(strongSrc, 'i');
      const soft = new RegExp(softSrc, 'i');
      const visible = e => { const r = e.getBoundingClientRect(); return r.width > 4 && r.height > 4; };
      const text = e => (e.innerText || e.textContent || '').trim().replace(/\s+/g, ' ');
      const leafish = e => e.children.length <= 3;
      const matchIn = list => {
        let el = list.find(e => visible(e) && leafish(e) && text(e) && text(e).length <= 60
                                && text(e) !== lastLabel && strong.test(text(e)));
        if (!el && allowSoft) {
          el = list.find(e => visible(e) && leafish(e) && text(e) && text(e).length <= 30
                              && text(e) !== lastLabel && soft.test(text(e)));
        }
        return el || null;
      };
      // Semantic controls first (proven to hit Instamart's real "Use current
      // location" button); div/span only as fallback for React div-CTAs.
      // Never re-click last round's label (no-progress guard).
      let el = matchIn([...document.querySelectorAll('button, a, [role="button"]')]);
      if (!el) el = matchIn([...document.querySelectorAll('div, span')]);
      if (!el) {
        // Diagnostics: what WAS clickable this round (traced, not guessed).
        if (window.__DSH_DEBUG_ONBOARD) {
          try {
            const all = [...document.querySelectorAll('button, a, [role="button"], div, span')];
            const opts = all.filter(e => visible(e) && text(e) && text(e).length <= 40)
                            .slice(0, 200)
                            .map(e => ({ tag: e.tagName.toLowerCase(), text: text(e) }));
            window.__DSH_DEBUG_ONBOARD.push({ options: opts });
          } catch (_) {}
        }
        return null;
      }
      const label = text(el).slice(0, 40);
      el.click();
      return label;
    }, [ONBOARD_STRONG, ONBOARD_SOFT, APP === 'instamart', lastLabel]).catch(() => null);
    if (!did) break;
    lastLabel = did;
    clicks++;
    console.error(`[onboarding] clicked "${did}" (${APP})`);
    // Reverse-geocode + sheet transitions can take a while (observed: the
    // address-confirm step appears 5-8s after "Use current location").
    await page.waitForTimeout(7000);
  }
  return clicks;
}

// Instamart's more reliable guest location flow (traced live 08-24): the
// "Use current location" button resolves a store but never shows a confirm
// step, while the address-search path ends in an explicit "Confirm Location"
// POST (select-location/v2 with the full address string). Steps, all the
// app's own UI: open "Search for an area or address" -> type the locality
// (reverse-geocoded by the app itself) -> tap the first suggestion -> tap
// "Confirm Location". NOTE 08-24: after heavy crawling our exit IP hit a
// LOGIN WALL ("Log in with phone number") — catalog + search 403 until a
// phone login, which we do not do. This flow still completes location
// onboarding for guest-friendly IPs/networks.
// Reverse-geocode our anchor to a human locality so we can drive Instamart's own
// "Add your location" modal. address-widgets/v2 (used on swiggy.com) does NOT
// fire on instamart.in, so we geocode client-side instead. Keyless BigDataCloud
// client endpoint; best-effort, returns null on any failure.
async function reverseGeocode(lat, lon) {
  try {
    const ac = new AbortController();
    const t = setTimeout(() => ac.abort(), 6000);
    const r = await fetch(
      `https://api.bigdatacloud.net/data/reverse-geocode-client?latitude=${lat}&longitude=${lon}&localityLanguage=en`,
      { signal: ac.signal });
    clearTimeout(t);
    if (!r.ok) return null;
    const j = await r.json();
    const term = [j.locality, j.city, j.principalSubdivision].filter(Boolean)[0];
    return typeof term === 'string' && term.length ? term : null;
  } catch (_) { return null; }
}

async function instamartAddressFlow(page, term) {
  const clickLeaf = (src, exact) => page.evaluate(([s, ex]) => {
    const rx = new RegExp(s, 'i');
    const els = [...document.querySelectorAll('button, a, [role="button"], div, span, p')];
    const cands = els.filter(e => {
      const t = (e.innerText || '').trim().replace(/\s+/g, ' ');
      if (!t || e.children.length > 3) return false;
      const r = e.getBoundingClientRect();
      if (r.width < 4 || r.height < 4) return false;
      return ex ? rx.test(t) && t.length <= 40 : rx.test(t) && t.length <= 60;
    });
    if (!cands.length) return null;
    cands.sort((a, b) => {
      const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
      return (ra.width * ra.height) - (rb.width * rb.height);
    });
    const label = cands[0].innerText.trim().replace(/\s+/g, ' ').slice(0, 50);
    cands[0].click();
    return label;
  }, [src, exact]).catch(() => null);

  let opened = await clickLeaf('search for (?:an area|a locality|your locality|an address|a street|locality)', false);
  if (!opened) {
    // Sheet not open (e.g. a prior "Use current location" click closed it):
    // reopen via the location bar, then retry.
    await clickLeaf('we deliver to|change your location|add your location', false);
    await page.waitForTimeout(2500);
    opened = await clickLeaf('^search for an area or address$', true);
  }
  if (!opened) return 0;
  console.error(`[onboarding] instamart: opened address search (term="${term}")`);
  await page.waitForTimeout(3000);

  const typed = await page.evaluate(t => {
    const inp = [...document.querySelectorAll('input')].find(i => {
      const r = i.getBoundingClientRect();
      return r.width > 4 && r.height > 4;
    });
    if (!inp) return false;
    inp.focus();
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    setter.call(inp, t);
    inp.dispatchEvent(new Event('input', { bubbles: true }));
    return true;
  }, term).catch(() => false);
  if (!typed) return 0;
  await page.waitForTimeout(4000);

  const firstWord = term.split(/\s+/)[0];
  const picked = await clickLeaf(firstWord, false);
  if (!picked) return 0;
  console.error(`[onboarding] instamart: picked suggestion "${picked}"`);
  await page.waitForTimeout(5000);

  const confirmed = await clickLeaf('^confirm location$', true);
  if (confirmed) console.error('[onboarding] instamart: confirmed location');
  return confirmed ? 1 : 0;
}

async function main() {
  const products = new Map();
  const apiHits = [];
  const apiLog = [];      // {url, status} of API-ish responses (stuck diagnostics)
  let imLocality = null;  // Instamart: reverse-geocoded locality from address-widgets
  let biggest = { url: '', len: 0, head: '' };
  let browser;
  try {
    browser = await chromium.launch({
      headless: true, channel: 'chromium',
      args: ['--disable-blink-features=AutomationControlled', '--lang=en-IN'],
    });
  } catch (_) {
    browser = await chromium.launch({ headless: true });
  }
  try {
    // Desktop context (no mobile emulation): verified 2025-08-30 that Zepto /
    // Blinkit return identical intercepted JSON vs a mobile profile, and
    // Instamart's gate is IP-based, so isMobile/hasTouch buy nothing. The data
    // comes from intercepted signed API JSON, not the rendered DOM, so a wider
    // desktop viewport only helps (more carousels render per page).
    const ctx = await browser.newContext({
      userAgent: UAS[Math.floor(Math.random() * UAS.length)],
      viewport: { width: 1366, height: 768 },
      deviceScaleFactor: 1,
      isMobile: false,
      hasTouch: false,
      geolocation: { latitude: LAT, longitude: LON },
      permissions: ['geolocation'],
      locale: 'en-IN',
      timezoneId: 'Asia/Kolkata',
    });
    await ctx.addInitScript(([lat, lon]) => {
      Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
      window.chrome = window.chrome || { runtime: {} };
      // Location seeding: these apps cache their serving location client-side
      // (Blinkit: localStorage.location{isDefault:true}=Gurugram + gr_1_* cookies
      // + cached 'merchant'), which overrides our GPS on every call. Seed OUR
      // anchor BEFORE any page script runs and drop the stale store binding.
      try {
        const loc = { coords: { isDefault: false, lat, lon, locality: null, id: null,
                                isTopCity: false, cityName: '', landmark: null,
                                addressId: null } };
        localStorage.setItem('location', JSON.stringify(loc));
        localStorage.removeItem('merchant');
        const ck = (n, v, keep) =>
          { document.cookie = `${n}=${v}; path=/; max-age=${keep ? 86400 : 0}`; };
        ck('gr_1_lat', lat, true);
        ck('gr_1_lon', lon, true);
        ck('gr_1_locality', '', false);
      } catch (_) {}
    }, [LAT, LON]);
    const page = await ctx.newPage();

    // Coordinate enforcement: apps often send a CACHED/default location (e.g.
    // Blinkit fired /visibility/latitude/28.41../longitude/77.07.. = NCR default
    // despite our GPS). Rewrite lat/lon in path segments, query params and JSON
    // POST bodies onto our target anchor so every signed call resolves THE
    // STORE WE WANT. Same mirror-the-app philosophy, applied to requests.
    await page.route('**/*', route => {
      const req = route.request();
      let url = req.url();
      try {
        if (/latitude|longitude/i.test(url)) {
          url = url.replace(/([/?&]latitude[=/])-?\d+(\.\d+)?/gi, `$1${LAT}`)
                   .replace(/([/?&]longitude[=/])-?\d+(\.\d+)?/gi, `$1${LON}`);
          const ur = new URL(url);
          let changed = false;
          for (const [k, v] of [['lat', LAT], ['latitude', LAT], ['longitude', LON], ['lng', LON], ['lon', LON]]) {
            if (ur.searchParams.has(k) && ur.searchParams.get(k) !== String(v)) {
              ur.searchParams.set(k, String(v));
              changed = true;
            }
          }
          if (changed) url = ur.toString();
        }
      } catch (_) {}
      let post = req.postData();
      if (post && /latitude/i.test(post)) {
        const before = post;
        post = post.replace(/("latitude"\s*:\s*)-?\d+(\.\d+)?/i, `$1${LAT}`)
                   .replace(/("(?:longitude|lng)"\s*:\s*)-?\d+(\.\d+)?/i, `$1${LON}`);
        if (post === before) post = req.postData();
      } else {
        post = req.postData();
      }
      if (url !== req.url() || post !== req.postData()) {
        return route.continue({ url, postData: post ?? undefined }).catch(() => {});
      }
      return route.continue().catch(() => {});
    });

    page.on('request', req => {
      const u = req.url();
      if (/api|catalog|search|listing|home|category/i.test(u) && !/\.(js|css|png|jpg|svg|woff)/i.test(u)) {
        apiHits.push({ url: u, method: req.method(), headers: req.headers(), post: req.postData() || null });
      }
    });
    page.on('response', async res => {
      try {
        const ru = res.url();
        if (/api|search|listing|home|category|store|location|serviceability/i.test(ru)
            && !/\.(js|css|png|jpg|svg|woff)/i.test(ru) && apiLog.length < 300) {
          apiLog.push({ url: ru.slice(0, 170), status: res.status() });
        }
        const body = await res.text();
        // Cap raised from 3MB: Instamart's store-gated collection/home responses
        // (home/v2 with a layoutId) routinely exceed 3MB and were being dropped
        // before parsing, so whole curated grids (MxN campaigns etc.) returned 0.
        if (!body || body.length < 50 || body.length > 12_000_000) return;
        if (DUMP && body.length > biggest.len) biggest = { url: res.url(), len: body.length, head: body.slice(0, 700) };
        const ct = (res.headers()['content-type'] || '');
        if (/json/i.test(ct)) {
          const j = JSON.parse(body);
          dumpBody(res.url(), body);
          extractMeta(j, 0);
          collect(j, products, 0);
          // Instamart onboarding: the app reverse-geocodes our anchor via
          // address-widgets; remember the locality name so the stuck-session
          // fallback can type it into the app's own address search box.
          if (!imLocality && /address-widgets/i.test(res.url())) {
            try {
              const md = (j && j.data && j.data.address && j.data.address.metadata) || {};
              const nm = [md.sublocality || md.locality, md.city].filter(Boolean).join(' ');
              if (nm) imLocality = nm;
            } catch (_) {}
          }
          return;
        }
        const m = body.match(/<script[^>]*id="__NEXT_DATA__"[^>]*>([\s\S]*?)<\/script>/);
        if (m) {
          const j = JSON.parse(m[1]);
          extractMeta(j, 0);
          collect(j, products, 0);
        }
      } catch (_) {}
    });

    // Instamart warm-up: bind a store on the homepage FIRST. Deep/collection
    // pages (e.g. /campaign-collection/mxn) do not expose the "Add your location"
    // trigger, so instamartAddressFlow() cannot open the modal there and they stay
    // store-less (no product fetch). The store set on the homepage (cookies /
    // localStorage) carries into the later goto() of the real target URL, so the
    // collection then fetches with store context.
    if (APP === 'instamart') {
      const warmTerm = imLocality || await reverseGeocode(LAT, LON);
      await page.goto('https://instamart.in/', { timeout: 30000, waitUntil: 'domcontentloaded' }).catch(() => {});
      await page.waitForTimeout(3000);
      if (warmTerm) {
        const ok = await instamartAddressFlow(page, warmTerm);
        if (ok) {
          console.error('[warmup] instamart: store bound on homepage, loading target with store context');
          await page.waitForTimeout(5000);
        }
      }
    }

    // Drop warm-up homepage products so only the TARGET page's products remain
    // (and receive the correct collection label below). The store context set
    // during warm-up carries over via cookies, so the target still fetches.
    if (APP === 'instamart') products.clear();

    // Label products from a store-gated Instamart collection (e.g. MxN campaign
    // grids) by their layoutId so Demand Radar can attribute them to the right
    // section instead of the generic "home" bucket.
    if (APP === 'instamart') {
      const mxn = (URL_.match(/campaign-collection\/mxn[?&][^]*layoutId=(\d+)/i) || [])[1];
      CURRENT_LABEL = mxn ? `mxn:${mxn}` : 'home';
    }

    await page.goto(URL_, { timeout: 30000, waitUntil: 'domcontentloaded' }).catch(() => {});
    const deadline = Date.now() + WAIT;
    let reloaded = false;
    while (Date.now() < deadline) {
      if (products.size > 0) break;
      const waf = apiHits.some(h => /awswaf/i.test(h.url));
      if (waf && !reloaded && Date.now() > deadline - WAIT + 8000) {
        try { await page.reload({ timeout: 30000, waitUntil: 'domcontentloaded' }); } catch (_) {}
        reloaded = true;
      }
      await page.waitForTimeout(2000);
    }
    await page.waitForTimeout(1500);

    // Instamart on instamart.in: the default catalog is NON-localized (no
    // store/ETA). Drive the app's own "Add your location" modal to bind the
    // anchor's locality so home_v2 returns a specific darkstore + ETA. Runs even
    // when products already exist (they're the default feed until we localize).
    let imTerm = null;
    if (APP === 'instamart') {
      imTerm = imLocality || await reverseGeocode(LAT, LON);
      if (imTerm) {
        console.error(`[localize] instamart: driving location modal (term="${imTerm}")`);
        const ok = await instamartAddressFlow(page, imTerm);
        if (ok) {
          // Store is now bound (cookies/localStorage). The Instamart SPA does NOT
          // auto-refetch the current route on store change, so a collection/page
          // that is store-gated (e.g. /campaign-collection/mxn) stays empty until
          // reloaded. Reload with the store context so products actually fetch.
          console.error('[localize] instamart: location confirmed, reloading with store context');
          await page.reload({ timeout: 30000, waitUntil: 'domcontentloaded' }).catch(() => {});
          await page.waitForTimeout(9000);
        }
      }
    }

    // Stuck at zero products? Drive the app's own onboarding/location flow
    // once (Instamart's consent sheet), then grant a fresh wait window.
    if (products.size === 0) {
      if (DEBUG_DIR) {
        await page.evaluate(() => { window.__DSH_DEBUG_ONBOARD = []; }).catch(() => {});
      }
      // Instamart FIRST choice (traced live 08-24): address-search ->
      // suggestion -> "Confirm Location" completes location setup with an
      // explicit confirm POST. The geolocation button closes the sheet
      // without a confirm step — which would also hide the "Search for an
      // area or address" entry this flow needs — so it runs second.
      let flowOk = 0;
      if (APP === 'instamart' && imTerm) {
        flowOk = await instamartAddressFlow(page, imTerm);
      }
      if (flowOk) {
        const deadline3 = Date.now() + Math.min(WAIT, 15000);
        while (Date.now() < deadline3 && products.size === 0) {
          await page.waitForTimeout(1500);
        }
      }
      if (products.size === 0) {
        const clicks = await clickThroughOnboarding(page);
        if (DEBUG_DIR) {
          const rounds = await page.evaluate(() => window.__DSH_DEBUG_ONBOARD || []).catch(() => []);
          try {
            fs.mkdirSync(DEBUG_DIR, { recursive: true });
            fs.writeFileSync(`${DEBUG_DIR}/onboard_rounds.json`, JSON.stringify(rounds, null, 1));
          } catch (_) {}
        }
        if (clicks > 0) {
          const deadline2 = Date.now() + Math.min(WAIT, 15000);
          while (Date.now() < deadline2 && products.size === 0) {
            await page.waitForTimeout(1500);
          }
        }
      }
    }

    // Stuck-session diagnostics: capture what the gate actually shows before
    // the visit queue runs (screenshot + leaf-ish clickable texts + API log).
    if (products.size === 0 && DEBUG_DIR) {
      try {
        fs.mkdirSync(DEBUG_DIR, { recursive: true });
        await page.screenshot({ path: `${DEBUG_DIR}/stuck.png` }).catch(() => {});
        const clickables = await page.evaluate(() => {
          const out = [];
          const els = document.querySelectorAll('button, a, [role="button"], [onclick], div, span, input');
          for (const e of els) {
            const tag = e.tagName.toLowerCase();
            const t = (e.innerText || e.value || '').trim().replace(/\s+/g, ' ');
            const r = e.getBoundingClientRect();
            if (r.width < 4 || r.height < 4) continue;
            if (tag === 'input') {
              out.push({ tag, type: e.type || '', placeholder: e.placeholder || '' });
              if (out.length >= 400) break;
              continue;
            }
            if (!t || t.length > 40 || e.children.length > 3) continue;
            out.push({ tag, role: e.getAttribute('role') || '', text: t });
            if (out.length >= 400) break;
          }
          return out;
        }).catch(() => []);
        fs.writeFileSync(`${DEBUG_DIR}/stuck_clickables.json`, JSON.stringify({
          url: page.url(), title: await page.title().catch(() => ''), clickables,
        }, null, 1));
        fs.writeFileSync(`${DEBUG_DIR}/stuck_api.json`, JSON.stringify(apiLog, null, 1));
        const html = await page.content().catch(() => '');
        fs.writeFileSync(`${DEBUG_DIR}/stuck.html`, html.slice(0, 500000));
        console.error(`[debug] stuck at 0 products — dump -> ${DEBUG_DIR} `
                      + `(${apiLog.length} api responses, ${clickables.length} elements)`);
      } catch (_) {}
    }

    // DOM fallback for HTML-rendered results (amazon/flipkart).
    if (products.size === 0) {
      for (const r of await domExtract(page)) {
        const mrp = num(r.mrpTxt);
        products.set(String(r.title).toLowerCase().replace(/\s+/g, '').slice(0, 60), {
          sku_key: String(r.title).toLowerCase().replace(/\s+/g, '').slice(0, 60),
          name: r.title.slice(0, 140), price: r.price,
          mrp: Number.isFinite(mrp) && mrp > r.price ? mrp : null,
          url: r.link || '',
        });
      }
    }

    // ---- deep-sweep visit queue: category links first, then searches ----
    const visits = [];
    if (CATEGORIES > 0) {
      const links = await page.evaluate(() => {
        const out = [];
        try {
          for (const a of document.querySelectorAll('a[href]')) {
            const h = a.getAttribute('href') || '';
            if (/^\/(cn|category|c)\//i.test(h)) {
              out.push({
                href: new URL(h, location.origin).toString(),
                text: (a.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 40),
              });
            }
          }
        } catch (_) {}
        return out;
      }).catch(() => []);
      const seenH = new Set();
      for (const l of links) {
        if (seenH.has(l.href)) continue;
        const lab = l.text || l.href.split('/').pop() || 'cat';
        if (SKIP.length && SKIP.some(s => lab.toLowerCase().includes(s))) {
          seenH.add(l.href);
          console.error(`[sweep] skip category "${lab.slice(0, 40)}"`);
          continue;
        }
        seenH.add(l.href);
        visits.push({ url: l.href, label: lab });
        if (visits.length >= CATEGORIES) break;
      }
    }
    for (const t of TERMS) {
      const build = SEARCH_URLS[APP];
      if (build) visits.push({ url: build(t), label: `q:${t}` });
    }
    for (const v of visits) {
      CURRENT_LABEL = v.label;
      try {
        await page.goto(v.url, { timeout: 25000, waitUntil: 'domcontentloaded' });
        await page.waitForTimeout(CAT_WAIT);
        console.error(`[sweep] ${v.label} -> cumulative ${products.size} SKUs`);
      } catch (_) {}
    }
    CURRENT_LABEL = 'home';

    // Mirror up to 3 captured signed calls for extra coverage.
    for (const hit of apiHits.slice(0, 3)) {
      try {
        const r = await page.request.fetch(hit.url, {
          method: hit.method, headers: hit.headers, data: hit.post, timeout: 10000,
        });
        if (r.status() === 200) collect(await r.json(), products, 0);
      } catch (_) {}
    }

    // Persist app state for later location-seeding experiments.
    if (BODY_DIR) {
      try {
        const ls = await page.evaluate(() => Object.fromEntries(Object.entries(localStorage)));
        fs.writeFileSync(`${BODY_DIR}/_local_storage.json`, JSON.stringify(ls, null, 2));
      } catch (_) {}
      try {
        fs.writeFileSync(`${BODY_DIR}/_cookies.json`, JSON.stringify(await ctx.cookies(), null, 2));
      } catch (_) {}
    }

    await ctx.close();
  } finally {
    await browser.close();
  }
  const list = [...products.values()].slice(0, 400);
  const topStores = [...META.stores.values()].sort((a, b) => b.count - a.count).slice(0, 6);
  console.log(JSON.stringify({ app: APP, url: URL_, lat: LAT, lon: LON,
                               api_endpoints_seen: apiHits.length,
                               store_hint: {
                                 candidates: topStores,
                                 eta_min: META.etas.length ? Math.min(...META.etas) : null,
                               },
                               source: list.length ? undefined : 'none',
                               dump: DUMP ? biggest : undefined, products: list }));
}

main().catch(e => console.log(JSON.stringify({ error: e.message, products: [] })));
