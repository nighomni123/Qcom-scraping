"""jiomart.py — JioMart adapter.

Initial adapter for JioMart quick-commerce. Browser-intercept pattern; jiomart.com +
/search?q= verified 200 via curl 08-31 (tinyfish confirmed the domain). Replicate
location seeding (localStorage.location, cookie/lat-lon rewrite) in
pw_catalog.js before trusting live crawls. See AGENTS.md invariants:
NULL ≠ OOS, debounce, streaks.
"""
from __future__ import annotations
import urllib.parse
from .base import Adapter


class JiomartAdapter(Adapter):
    name = "jiomart"

    APP_URL = "https://www.jiomart.com/"
    HEALTH_URL = "https://www.jiomart.com/search?q=milk"
    PROBE_URL = HEALTH_URL
    PROBE_TERMS = ("milk", "bread", "rice", "oil", "shampoo", "paan",
                   "cigarette", "condom", "chips", "detergent")

    def search(self, query, lat=None, lon=None):
        url = "https://www.jiomart.com/search?q=" + urllib.parse.quote_plus(query)
        return self._browser_catalog(url, "jiomart::search", "jiomart", lat, lon)

    def crawl(self, station, lat, lon):
        products = []
        for h in [x for x in self.honey if x.get("app") == "jiomart"]:
            q = h.get("query", "")
            if q:
                search_url = f"https://www.jiomart.com/search?q={q.replace(' ', '%20')}"
                products += self._browser_catalog(search_url, f"jiomart::{station}", "jiomart", lat, lon)
        for t in self.rotating_terms(1):
            search_url = "https://www.jiomart.com/search?q=" + urllib.parse.quote_plus(t)
            products += self._browser_catalog(search_url, f"jiomart::{station}", "jiomart", lat, lon)
        products += self._browser_catalog(self.APP_URL, f"jiomart::{station}", "jiomart", lat, lon)
        return products
