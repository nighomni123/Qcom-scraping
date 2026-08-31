"""bigbasket.py — BigBasket adapter.

Initial adapter for BigBasket (bbnow). Uses browser-intercept path like
existing QC adapters: real mobile web app, geolocation anchored per point,
mirrors signed catalog calls. URL patterns are initial guesses; verify
via --qc-status / live sweep and adjust APP_URL / HEALTH_URL / search route
as needed (see AGENTS.md: replicate location seeding + lat/lon rewrite in
pw_catalog.js when the exact route shapes are confirmed).
"""
from __future__ import annotations
import urllib.parse
from .base import Adapter


class BigbasketAdapter(Adapter):
    name = "bigbasket"

    APP_URL = "https://www.bigbasket.com/"
    HEALTH_URL = "https://www.bigbasket.com/search?q=milk"
    PROBE_URL = HEALTH_URL
    PROBE_TERMS = ("milk", "bread", "rice", "oil", "shampoo", "paan",
                   "cigarette", "condom", "chips", "detergent")

    def search(self, query, lat=None, lon=None):
        url = "https://www.bigbasket.com/search?q=" + urllib.parse.quote_plus(query)
        return self._browser_catalog(url, "bigbasket::search", "bigbasket", lat, lon)

    def crawl(self, station, lat, lon):
        products = []
        for h in [x for x in self.honey if x.get("app") == "bigbasket"]:
            q = h.get("query", "")
            if q:
                search_url = f"https://www.bigbasket.com/search?q={q.replace(' ', '%20')}"
                products += self._browser_catalog(search_url, f"bigbasket::{station}", "bigbasket", lat, lon)
        for t in self.rotating_terms(1):
            search_url = "https://www.bigbasket.com/search?q=" + urllib.parse.quote_plus(t)
            products += self._browser_catalog(search_url, f"bigbasket::{station}", "bigbasket", lat, lon)
        products += self._browser_catalog(self.APP_URL, f"bigbasket::{station}", "bigbasket", lat, lon)
        return products
