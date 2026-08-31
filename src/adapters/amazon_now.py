"""amazon_now.py — Amazon Now adapter.

Amazon Now (Amazon Fresh / quick delivery surface) adapter. Initial URL
patterns based on Amazon.in fresh routes. Replicate both location seeding
layers once verified: localStorage + cookie lat/lon rewrite, plus request-body
rewrites for Amazon's location gating (see AGENTS.md notes on other apps).
"""
from __future__ import annotations
import urllib.parse
from .base import Adapter


class AmazonNowAdapter(Adapter):
    name = "amazon_now"

    APP_URL = "https://www.amazon.in/alm/storefront"
    HEALTH_URL = "https://www.amazon.in/alm/storefront"
    PROBE_URL = HEALTH_URL
    PROBE_TERMS = ("milk", "bread", "rice", "oil", "shampoo", "paan",
                   "cigarette", "condom", "chips", "detergent")

    def search(self, query, lat=None, lon=None):
        url = "https://www.amazon.in/s?k=" + urllib.parse.quote_plus(query) + "&i=fresh"
        return self._browser_catalog(url, "amazon_now::search", "amazon_now", lat, lon)

    def crawl(self, station, lat, lon):
        products = []
        for h in [x for x in self.honey if x.get("app") == "amazon_now"]:
            q = h.get("query", "")
            if q:
                search_url = f"https://www.amazon.in/s?k={q.replace(' ', '%20')}&i=fresh"
                products += self._browser_catalog(search_url, f"amazon_now::{station}", "amazon_now", lat, lon)
        for t in self.rotating_terms(1):
            search_url = "https://www.amazon.in/s?k=" + urllib.parse.quote_plus(t) + "&i=fresh"
            products += self._browser_catalog(search_url, f"amazon_now::{station}", "amazon_now", lat, lon)
        products += self._browser_catalog(self.APP_URL, f"amazon_now::{station}", "amazon_now", lat, lon)
        return products
