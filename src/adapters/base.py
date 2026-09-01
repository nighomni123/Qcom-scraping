"""
adapters/base.py — shared anti-block helpers + the Adapter interface.

The big idea: we do NOT guess the apps' rotating, signed API endpoints. Instead
we run the REAL mobile web app in a headless browser, intercept its network
calls to the catalog API, and replay those *exact* signed requests on a tight
loop. When the app rotates the path/headers, the next page load re-discovers
them. The app is the API documentation.

For environments without Playwright (or where the browser layer is disabled),
adapters degrade gracefully: they return [] and the orchestrator keeps the
trackers + detection engine running.
"""
from __future__ import annotations

import json
import os
import random
import signal
import string
import subprocess
import sys
import threading
import time

MOBILE_UAS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Mobile Safari/537.36",
]


def fresh_user_agent() -> str:
    return random.choice(MOBILE_UAS)


def fresh_install_id() -> str:
    # Plausible install/device id the apps expect.
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=16))


def load_proxies(env=None) -> list:
    raw = (env or {}).get("PROXY_URL") or os.environ.get("PROXY_URL", "")
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def pick_proxy(proxies):
    return random.choice(proxies) if proxies else None


def norm_price(text):
    """Extract a rupee price from messy text."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    import re
    m = re.search(r"₹?\s?([0-9]+(?:\.[0-9]{1,2})?)", str(text))
    return float(m.group(1)) if m else None


class Adapter:
    """Subclasses implement crawl() -> list of product dicts."""

    name = "base"
    # Product-bearing URL for one-shot probes: apps whose HOME serves no
    # products for fresh sessions (Zepto's home is layout-only) override this
    # with their search route. Falls back to APP_URL.
    PROBE_URL = None
    # Small search-term set fired after the initial probe load, INSIDE the same
    # browser session, so one-shot indexing sees more than the home feed's
    # first carousels (Blinkit serves dairy-first). Deliberately NOT used by
    # the continuous monitor loop (crawl()) to keep steady-state volume low.
    PROBE_TERMS = ()

    def __init__(self, cfg, geo_corridor, honey=None):
        self.cfg = cfg
        self.corridor = geo_corridor
        self.honey = honey or []
        self.antiblk = cfg.get("anti_block", {})
        # Proxy pool = config anti_block.proxies + PROXY_URL env (comma list).
        # Empty by default → browser path unchanged (direct connection).
        cfg_proxies = [str(p).strip() for p in (self.antiblk.get("proxies") or [])
                       if str(p).strip()]
        self.proxies = cfg_proxies + load_proxies()
        self.available = True

    def _rotate(self):
        ua = fresh_user_agent() if self.antiblk.get("rotate_user_agent", True) else MOBILE_UAS[0]
        iid = fresh_install_id() if self.antiblk.get("rotate_install_id", True) else "static0000000001"
        return ua, iid, pick_proxy(self.proxies)

    def crawl(self, station, lat, lon):
        """Return list of dicts: {sku_key, name, price, mrp, url}."""
        raise NotImplementedError

    def search(self, query, lat=None, lon=None):
        """Search this platform for a product. Default: no search support."""
        return []

    # ---- shared browser-intercept machinery ----
    def health_probe(self, station, lat, lon):
        """
        One probe for --qc-status. Defaults to APP_URL; apps whose home page
        serves no products (Zepto's home is layout-only — products need the
        search route) override HEALTH_URL.
        """
        url = getattr(self, "HEALTH_URL", None) or getattr(self, "APP_URL", None)
        if not url:
            return [], {"error": f"{self.name}: no APP_URL defined"}
        return self._browser_catalog_full(url, f"{self.name}::health", self.name, lat, lon)

    def probe_point(self, station, lat, lon):
        """
        One PRODUCT-BEARING probe at an anchor point (Demand Radar locality
        mapping + --store-inventory). Uses PROBE_URL when the app's home page
        serves no products (Zepto), then fires PROBE_TERMS in the same browser
        session for catalog breadth beyond the home feed's carousels.
        Returns (products, meta). meta carries darkstore-identity candidates +
        delivery ETA extracted from the intercepted API traffic.
        """
        url = getattr(self, "PROBE_URL", None) or getattr(self, "APP_URL", None)
        if not url:
            return [], {"error": f"{self.name}: no APP_URL defined"}
        return self._browser_catalog_full(url, f"{self.name}::{station}", self.name,
                                          lat, lon, terms=tuple(self.PROBE_TERMS))

    def rotating_terms(self, n=1):
        """Next n queries from schedule.crawl_terms (round-robin per adapter
        process). Lets the glitch monitor's coverage diversify beyond the
        dairy-first home carousel + honey canaries without growing per-cycle
        volume by more than n launches. Empty/absent list = off."""
        terms = [str(t).strip() for t in
                 ((self.cfg.get("schedule", {}) or {}).get("crawl_terms") or [])
                 if str(t).strip()]
        if not terms:
            return []
        n = max(1, min(int(n), len(terms)))
        i = getattr(self, "_term_ix", 0)
        self._term_ix = (i + n) % len(terms)
        return [terms[(i + k) % len(terms)] for k in range(n)]

    def deep_sweep(self, station, lat, lon, categories=0, terms=None,
                   deep_cats=False, skip_override=None):
        """
        Demand Radar watchlist sweep for ONE store anchor in ONE browser
        session: home harvest -> optional DOM category click-through ->
        staple search terms. Everything is intercepted live from the real app,
        so products arrive signed + stock-stamped, labeled via `collections`
        ('home', category names, 'q:<term>').
        demand.skip_categories (config) filters the category queue by label
        substring — the apps' category rails are dairy-first, so without it
        the sweep over-samples milk products. Catalog-inventory mode passes
        skip_override=[] (FULL coverage — a skipped category would fabricate
        delistings in snapshot diffs) and deep_cats=True (one-hop
        sub-category discovery, see pw_catalog.js --deep-cats).
        """
        url = getattr(self, "APP_URL", None)
        if not url:
            return [], {"error": f"{self.name}: no APP_URL defined"}
        skip = skip_override
        if skip is None:
            skip = [str(s).strip() for s in
                    ((self.cfg.get("demand", {}) or {}).get("skip_categories") or [])
                    if str(s).strip()]
        return self._browser_catalog_full(url, f"{self.name}::{station}", self.name,
                                          lat, lon, categories=categories, terms=terms,
                                          skip=skip or None, deep_cats=deep_cats)

    def _browser_catalog(self, url, store_id, app_label, lat=None, lon=None, pre=None):
        """Compat wrapper returning products only; see _browser_catalog_full."""
        products, _meta = self._browser_catalog_full(url, store_id, app_label, lat=lat, lon=lon,
                                                     pre=pre)
        return products

    def _browser_catalog_full(self, url, store_id, app_label, lat=None, lon=None,
                              categories=0, terms=None, skip=None, pre=None,
                              deep_cats=False):
        """
        Run the real app in headless chromium (via the Node helper in tools/),
        intercept + mirror its signed catalog calls. Returns (products, meta).

        meta = {store_candidates: [{key,value,count,label}], eta_min: float|None,
                api_endpoints_seen: int} — or {"error": "..."} on failure.
        categories/terms drive the deep-sweep visit queue (see deep_sweep).
        Uses the pre-installed Playwright browsers; degrades to ([], error) if
        Node or the browser is missing.
        """
        import json as _json
        helper = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "tools", "pw_catalog.js")
        if not os.path.exists(helper):
            self.available = False
            return [], {"error": "pw_catalog.js missing"}
        env = dict(os.environ)
        node_path = self.antiblk.get("node_path")
        pw_path = self.antiblk.get("pw_browsers_path")
        if node_path:
            env["NODE_PATH"] = node_path
        if pw_path:
            env["PLAYWRIGHT_BROWSERS_PATH"] = pw_path
        cmd = ["node", helper, "--app", app_label, "--url", url,
               "--lat", str(lat if lat is not None else 19.119),
               "--lon", str(lon if lon is not None else 72.846)]
        terms = [t for t in (terms or []) if t]
        extra_visits = int(categories or 0) + len(terms) + len(pre or [])
        if categories:
            cmd += ["--categories", str(int(categories))]
        if deep_cats:
            cmd += ["--deep-cats", "1"]
        if terms:
            cmd += ["--terms", "|".join(terms)]
        if skip:
            cmd += ["--skip", "|".join(skip)]
        if pre:
            # Verticals to visit BEFORE the target, "URL::LABEL" pairs joined
            # by '|' — see tools/pw_catalog.js PRE comment (Blinkit tobacco:
            # text search serves none, direct shelf URLs serve it all).
            cmd += ["--pre", "|".join(f"{u}::{lab}" for u, lab in pre)]
        # Phase 5: route this run through a residential proxy so the request IP
        # matches the GPS anchor (QC apps resolve the darkstore from IP). One
        # proxy per invocation == one fresh browser+context == one clean WAF
        # identity (never rotate under a live context). Empty pool → no --proxy.
        proxy = pick_proxy(self.proxies)
        if proxy:
            cmd += ["--proxy", proxy]
        timeout_s = 90 + extra_visits * 15
        # Live progress: forward the helper's stderr (per-visit [sweep] lines,
        # onboarding/localize steps) as it works instead of swallowing it until
        # the multi-minute sweep ends. Disable via anti_block.stream_progress.
        stream = bool(self.antiblk.get("stream_progress", True))
        try:
            # Own process group so the timeout kill also reaps the browser
            # grandchildren — otherwise they outlive the helper holding the
            # pipe write-ends and the reader threads block forever.
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1, env=env,
                **({"start_new_session": True} if os.name == "posix" else {}),
            )
        except FileNotFoundError:
            print(f"[warn] node not found — {app_label} browser adapter offline")
            self.available = False
            return [], {"error": "node-not-found"}
        except Exception as ex:
            print(f"[warn] {app_label} browser crawl failed to start: {str(ex)[:140]}")
            self.available = False
            return [], {"error": str(ex)[:200]}
        # Drain BOTH pipes on background threads: the final JSON line can
        # exceed the OS pipe buffer, so stdout must be consumed concurrently
        # with stderr or the helper would deadlock writing it. The main thread
        # waits on the PROCESS (not the pipes): a killed helper can leave
        # grandchildren (the browser) holding the pipe write-ends, which would
        # block a pipe-EOF read forever — wait() returns as soon as the helper
        # itself is gone.
        out_chunks = []

        def _drain(fh, sink, prefix=None):
            try:
                for line in fh:
                    line = line.rstrip("\n")
                    if not line:
                        continue
                    if sink is not None:
                        sink.append(line)
                    elif stream:
                        print(f"{prefix}{line}", file=sys.stderr, flush=True)
            except Exception:
                pass

        threads = [threading.Thread(target=_drain, args=(proc.stdout, out_chunks), daemon=True),
                   threading.Thread(target=_drain, args=(proc.stderr, None, f"[{app_label}] "), daemon=True)]
        for t in threads:
            t.start()
        timed_out = {"v": False}

        def _kill():
            timed_out["v"] = True
            try:
                if os.name == "posix":
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                else:
                    proc.kill()
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        timer = threading.Timer(timeout_s, _kill)
        timer.daemon = True
        timer.start()
        try:
            proc.wait()
        finally:
            timer.cancel()
        # Helper gone (or killed). Unblock the drainers in case grandchildren
        # still hold the pipe write-ends, then give them a moment to finish.
        for fh in (proc.stdout, proc.stderr):
            try:
                fh.close()
            except Exception:
                pass
        for t in threads:
            t.join(timeout=10)
        drained = "".join(out_chunks)
        if timed_out["v"]:
            print(f"[warn] {app_label} browser crawl timed out after {timeout_s}s")
            self.available = False
            return [], {"error": f"timeout after {timeout_s}s"}
        data = _json.loads(drained.strip().splitlines()[-1]) if drained.strip() else {}
        if data.get("error"):
            print(f"[{app_label}] browser: {data['error'][:120]}")
            self.available = False
            return [], {"error": str(data["error"])[:200]}
        prods = data.get("products", [])
        hint = data.get("store_hint") or {}
        meta = {
            "store_candidates": hint.get("candidates", []),
            "eta_min": hint.get("eta_min"),
            "api_endpoints_seen": data.get("api_endpoints_seen", 0),
            "resolved_lat": data.get("lat"),
            "resolved_lon": data.get("lon"),
        }
        if prods:
            n_stock = sum(1 for p in prods if p.get("in_stock") is not None)
            print(f"[{app_label}] browser-intercept ok @ {store_id}: {len(prods)} products "
                  f"({n_stock} w/ stock state), eta={meta['eta_min']}, "
                  f"{meta['api_endpoints_seen']} api endpoints seen")
        return prods, meta


_STOCK_KEYS = ("in_stock", "instock", "is_available", "available", "availability",
               "out_of_stock", "oos", "stock", "sold_out", "stock_status")
_STOCK_INVERT = {"out_of_stock", "oos", "sold_out"}


def _stock_state(key, value):
    """Stock-ish key/value -> True|False|None (None = unknown)."""
    if isinstance(value, dict):
        value = value.get("status") or value.get("in_stock") or value.get("value")
        if isinstance(value, dict):
            return None
    b = None
    if isinstance(value, bool):
        b = value
    elif isinstance(value, (int, float)):
        b = value != 0
    elif isinstance(value, str):
        s = value.strip().lower()
        if s in ("true", "yes", "y", "in_stock", "instock", "available", "in", "1"):
            b = True
        elif s in ("false", "no", "n", "out_of_stock", "oos", "unavailable", "sold_out", "out", "0"):
            b = False
    if b is None:
        return None
    return (not b) if key.lower() in _STOCK_INVERT else b


def _parse_catalog(app_label, text, out):
    """Best-effort parse of various catalog shapes into normalized products."""
    try:
        data = json.loads(text)
    except Exception:
        return
    # Walk for price/mrp/name-ish fields; apps nest differently.
    found = []

    def walk(o):
        if isinstance(o, dict):
            price = o.get("price") or o.get("final_price") or o.get("selling_price")
            name = o.get("name") or o.get("title") or o.get("product_name")
            mrp = o.get("mrp") or o.get("marked_price") or o.get("original_price")
            if price and name:
                stock = None
                for k in _STOCK_KEYS:
                    if k in o:
                        s = _stock_state(k, o[k])
                        if s is not None:
                            stock = s
                found.append({
                    "sku_key": str(o.get("id") or name).lower().replace(" ", ""),
                    "name": name, "price": float(price),
                    "mrp": float(mrp) if mrp else None,
                    "url": o.get("url") or o.get("link") or "",
                    "in_stock": stock,
                })
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(data)
    out["products"] = found
