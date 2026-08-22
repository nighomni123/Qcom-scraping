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
const CAT_WAIT = parseInt(arg('cat-wait-ms', '6500'), 10);

// Label attached to everything intercepted while visiting the current page.
let CURRENT_LABEL = 'home';

// Search URL shapes per app (stable UI routes — mirrors src/adapters/*.py).
const SEARCH_URLS = {
  'blinkit':   t => `https://blinkit.com/s/?q=${encodeURIComponent(t)}`,
  'zepto':     t => `https://www.zeptonow.com/search?q=${encodeURIComponent(t)}`,
  'instamart': t => `https://www.swiggy.com/instamart/search?query=${encodeURIComponent(t)}`,
};

const UAS = [
  'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1',
  'Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36',
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
                    'out_of_stock', 'oos', 'stock', 'sold_out', 'stock_status',
                    'inventory', 'is_sold_out', 'product_state'];
const STOCK_INVERT = new Set(['out_of_stock', 'oos', 'sold_out', 'is_sold_out']);
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
    v = v.status ?? v.in_stock ?? v.value ?? v.count ?? v.availability;
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
                            'vendor_id', 'vendorid', 'dc_id', 'store', 'warehouse'];
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
  if (!node || typeof node !== 'object' || depth > 9) return;
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
  if (!node || typeof node !== 'object' || depth > 9) return;
  if (Array.isArray(node)) { for (const v of node) collect(v, out, depth + 1); return; }
  const keys = Object.keys(node);
  const lower = k => k.toLowerCase();
  const nameKey = keys.find(k => ['name', 'title', 'product_name', 'display_name'].includes(lower(k)));
  const priceKey = keys.find(k => ['price', 'final_price', 'selling_price', 'discounted_price', 'offer_price'].includes(lower(k)));
  const mrpKey = keys.find(k => ['mrp', 'marked_price', 'original_price', 'list_price'].includes(lower(k)));
  if (nameKey && priceKey) {
    // Blinkit feed nests name/mrp as styled objects ({text:"₹75", ...}) — unwrap.
    let name = node[nameKey];
    if (name && typeof name === 'object') name = name.text ?? name.display_name ?? null;
    let price = node[priceKey];
    if (price && typeof price === 'object') price = price.value ?? price.amount ?? null;
    price = num(price);
    const mrpRaw = mrpKey ? node[mrpKey] : null;
    const mrp = num(mrpRaw && typeof mrpRaw === 'object' ? (mrpRaw.text ?? mrpRaw.value) : mrpRaw);
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
      const id = node.id ?? node.sku ?? node.product_id ?? name;
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
  'www.zeptonow.com': () => {
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

function domExtract(page) {
  const host = new URL(URL_).hostname.replace(/^www\./, '');
  const key = Object.keys(DOM_EXTRACTORS).find(h => host === h.replace(/^www\./, '') || host.endsWith(h));
  const fn = DOM_EXTRACTORS[key];
  if (!fn) return [];
  return page.evaluate(fn).catch(() => []);
}

async function main() {
  const products = new Map();
  const apiHits = [];
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
    const ctx = await browser.newContext({
      userAgent: UAS[Math.floor(Math.random() * UAS.length)],
      viewport: { width: 390, height: 844 },
      deviceScaleFactor: 3,
      isMobile: true,
      hasTouch: true,
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
        const body = await res.text();
        if (!body || body.length < 50 || body.length > 3_000_000) return;
        if (DUMP && body.length > biggest.len) biggest = { url: res.url(), len: body.length, head: body.slice(0, 700) };
        const ct = (res.headers()['content-type'] || '');
        if (/json/i.test(ct)) {
          const j = JSON.parse(body);
          dumpBody(res.url(), body);
          extractMeta(j, 0);
          collect(j, products, 0);
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
        seenH.add(l.href);
        visits.push({ url: l.href, label: l.text || l.href.split('/').pop() || 'cat' });
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
