"""bigbasket.py — BigBasket adapter.

Initial adapter for BigBasket (bbnow). Uses browser-intercept path like
existing QC adapters: real mobile web app, geolocation anchored per point,
mirrors signed catalog calls. Routes verified 08-31: home bigbasket.com serves 200; the search route is
/ps/?q=<query> (200) — /search?q= 403s. Replicate location seeding + lat/lon
rewrite in pw_catalog.js before trusting live crawls (see AGENTS.md).
"""
from __future__ import annotations
import urllib.parse
from .base import Adapter


class BigbasketAdapter(Adapter):
    name = "bigbasket"

    APP_URL = "https://www.bigbasket.com/"
    HEALTH_URL = "https://www.bigbasket.com/ps/?q=milk"
    PROBE_URL = HEALTH_URL
    PROBE_TERMS = ("milk", "bread", "rice", "oil", "shampoo", "paan",
                   "cigarette", "condom", "chips", "detergent")

    def search(self, query, lat=None, lon=None):
        url = "https://www.bigbasket.com/ps/?q=" + urllib.parse.quote_plus(query)
        return self._browser_catalog(url, "bigbasket::search", "bigbasket", lat, lon)

    def crawl(self, station, lat, lon):
        products = []
        for h in [x for x in self.honey if x.get("app") == "bigbasket"]:
            q = h.get("query", "")
            if q:
                search_url = f"https://www.bigbasket.com/ps/?q={q.replace(' ', '%20')}"
                products += self._browser_catalog(search_url, f"bigbasket::{station}", "bigbasket", lat, lon)
        for t in self.rotating_terms(1):
            search_url = "https://www.bigbasket.com/ps/?q=" + urllib.parse.quote_plus(t)
            products += self._browser_catalog(search_url, f"bigbasket::{station}", "bigbasket", lat, lon)
        products += self._browser_catalog(self.APP_URL, f"bigbasket::{station}", "bigbasket", lat, lon)
        return products
