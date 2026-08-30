"""zepto.py — Zepto adapter.

Zepto (08-22): zeptonow.com 301s to www.zepto.com, and the search route reads
?query= (NOT ?q= — wrong param loads a home shell and never fires
user-search-service/api/v3/search). Prices arrive in paise; pw_catalog.js
normalizes to rupees. Same browser-intercept strategy: real app in a mobile
fingerprint, geolocation anchored per point, mirror signed catalog calls.
"""
from __future__ import annotations
import urllib.parse
from .base import Adapter


class ZeptoAdapter(Adapter):
    name = "zepto"

    APP_URL = "https://www.zepto.com/"
    # Home is layout-only (no product widgets for fresh sessions) — health
    # checks and any product probing go through the search route.
    HEALTH_URL = "https://www.zepto.com/search?query=milk"
    PROBE_URL = HEALTH_URL
    # Breadth beyond the first search: staples + non-food / paan-shop SKUs, same
    # browser session, so --store-inventory captures convenience + tobacco too.
    PROBE_TERMS = ("atta", "shampoo", "paan", "cigarette", "gutkha",
                   "pan masala", "tobacco", "condom", "mukhwas")

    def search(self, query, lat=None, lon=None):
        url = "https://www.zepto.com/search?query=" + urllib.parse.quote_plus(query)
        return self._browser_catalog(url, "zepto::search", "zepto", lat, lon)

    def crawl(self, station, lat, lon):
        products = []
        for h in [x for x in self.honey if x.get("app") == "zepto"]:
            q = h.get("query", "")
            if q:
                search_url = f"https://www.zepto.com/search?query={q.replace(' ', '%20')}"
                products += self._browser_catalog(search_url, f"zepto::{station}", "zepto", lat, lon)
        # Rotating coverage terms (schedule.crawl_terms): diversify price_obs
        # beyond the dairy-first home carousel + honey canaries.
        for t in self.rotating_terms(1):
            search_url = "https://www.zepto.com/search?query=" + urllib.parse.quote_plus(t)
            products += self._browser_catalog(search_url, f"zepto::{station}", "zepto", lat, lon)
        products += self._browser_catalog(self.APP_URL, f"zepto::{station}", "zepto", lat, lon)
        return products
