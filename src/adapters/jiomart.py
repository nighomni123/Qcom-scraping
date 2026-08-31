"""jiomart.py — JioMart adapter.

Browser-intercept pattern; jiomart.com + /search?q= verified 08-31.
EXTRACTION WORKS (pw_catalog.js harvests products with price/MRP/stock/
store_ids from the ext/vertex products API — field names traced 08-31 from
DSH_BODY_DIR dumps). But the QC darkstore is resolved SERVER-SIDE from the
request IP (delivery-promise returned the same store at an identical
distance across runs whose app_geolocation cookie held different values), so
it CANNOT be steered to Demand-Radar anchors — client-side seeding does
nothing, don't add it. Stays `enabled: false` (see AGENTS.md "Expansion
apps" + README "Adapter expansion"): at best a machine-location
--store-inventory source, and its search endpoint rate-limits hard.
Invariants still apply: NULL ≠ OOS, debounce, streaks.
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
