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
import string
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

    def __init__(self, cfg, geo_corridor, honey=None):
        self.cfg = cfg
        self.corridor = geo_corridor
        self.honey = honey or []
        self.antiblk = cfg.get("anti_block", {})
        self.proxies = load_proxies()
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
        One home-load probe at an anchor point (Demand Radar locality mapping).
        Returns (products, meta). meta carries darkstore-identity candidates +
        delivery ETA extracted from the intercepted API traffic.
        """
        url = getattr(self, "APP_URL", None)
        if not url:
            return [], {"error": f"{self.name}: no APP_URL defined"}
        return self._browser_catalog_full(url, f"{self.name}::{station}", self.name, lat, lon)

    def deep_sweep(self, station, lat, lon, categories=0, terms=None):
        """
        Demand Radar watchlist sweep for ONE store anchor in ONE browser
        session: home harvest -> optional DOM category click-through ->
        staple search terms. Everything is intercepted live from the real app,
        so products arrive signed + stock-stamped, labeled via `collections`
        ('home', category names, 'q:<term>').
        """
        url = getattr(self, "APP_URL", None)
        if not url:
            return [], {"error": f"{self.name}: no APP_URL defined"}
        return self._browser_catalog_full(url, f"{self.name}::{station}", self.name,
                                          lat, lon, categories=categories, terms=terms)

    def _browser_catalog(self, url, store_id, app_label, lat=None, lon=None):
        """Compat wrapper returning products only; see _browser_catalog_full."""
        products, _meta = self._browser_catalog_full(url, store_id, app_label, lat=lat, lon=lon)
        return products

    def _browser_catalog_full(self, url, store_id, app_label, lat=None, lon=None,
                              categories=0, terms=None):
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
        import subprocess
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
        extra_visits = int(categories or 0) + len(terms)
        if categories:
            cmd += ["--categories", str(int(categories))]
        if terms:
            cmd += ["--terms", "|".join(terms)]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=90 + extra_visits * 15, env=env)
            data = _json.loads(out.stdout.strip().splitlines()[-1]) if out.stdout.strip() else {}
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
        except FileNotFoundError:
            print(f"[warn] node not found — {app_label} browser adapter offline")
            self.available = False
            return [], {"error": "node-not-found"}
        except Exception as ex:
            print(f"[warn] {app_label} browser crawl failed: {str(ex)[:140]}")
            self.available = False
            return [], {"error": str(ex)[:200]}


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
