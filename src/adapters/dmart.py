"""dmart.py — DMart Ready adapter.

DMart Ready (quick-commerce / grocery delivery) adapter. Uses the same
browser-intercept strategy as existing QC apps: load the real mobile web app,
intercept signed catalog responses, replicate location seeding. Routes
verified 08-31: dmartready.com 301s to www.dmart.in; /search?q= serves 200.
Implement pw_catalog.js location seeding before trusting live crawls.
"""
from __future__ import annotations
import urllib.parse
from .base import Adapter


class DmartAdapter(Adapter):
    name = "dmart"

    APP_URL = "https://www.dmart.in/"
    HEALTH_URL = "https://www.dmart.in/search?q=milk"
    PROBE_URL = HEALTH_URL
    PROBE_TERMS = ("milk", "bread", "rice", "oil", "shampoo", "paan",
                   "cigarette", "condom", "chips", "detergent")

    def search(self, query, lat=None, lon=None):
        url = "https://www.dmart.in/search?q=" + urllib.parse.quote_plus(query)
        return self._browser_catalog(url, "dmart::search", "dmart", lat, lon)

    def crawl(self, station, lat, lon):
        products = []
        for h in [x for x in self.honey if x.get("app") == "dmart"]:
            q = h.get("query", "")
            if q:
                search_url = f"https://www.dmart.in/search?q={q.replace(' ', '%20')}"
                products += self._browser_catalog(search_url, f"dmart::{station}", "dmart", lat, lon)
        for t in self.rotating_terms(1):
            search_url = "https://www.dmart.in/search?q=" + urllib.parse.quote_plus(t)
            products += self._browser_catalog(search_url, f"dmart::{station}", "dmart", lat, lon)
        products += self._browser_catalog(self.APP_URL, f"dmart::{station}", "dmart", lat, lon)
        return products
