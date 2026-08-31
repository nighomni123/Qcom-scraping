"""amazon_now.py — Amazon Now adapter.

Amazon Now is Amazon India's quick-commerce brand (Amazon Fresh folded into
it). Routes verified 08-31 via tinyfish search + curl: the web surface is the
"10 Minutes Delivery" search route — /10-minutes-delivery/s?k=<query> serves
200 (the bare /10-minutes-delivery path 404s), and /fresh 301s to the
alm/storefront (almBrandId=ctnow). Replicate both location-seeding layers in
pw_catalog.js before trusting live crawls: localStorage + cookie lat/lon
rewrite, plus request-body rewrites for Amazon's location gating (see
AGENTS.md notes on other apps).
"""
from __future__ import annotations
import urllib.parse
from .base import Adapter


class AmazonNowAdapter(Adapter):
    name = "amazon_now"

    APP_URL = "https://www.amazon.in/alm/storefront?almBrandId=ctnow"
    HEALTH_URL = "https://www.amazon.in/10-minutes-delivery/s?k=milk"
    PROBE_URL = HEALTH_URL
    PROBE_TERMS = ("milk", "bread", "rice", "oil", "shampoo", "paan",
                   "cigarette", "condom", "chips", "detergent")

    def search(self, query, lat=None, lon=None):
        url = "https://www.amazon.in/10-minutes-delivery/s?k=" + urllib.parse.quote_plus(query)
        return self._browser_catalog(url, "amazon_now::search", "amazon_now", lat, lon)

    def crawl(self, station, lat, lon):
        products = []
        for h in [x for x in self.honey if x.get("app") == "amazon_now"]:
            q = h.get("query", "")
            if q:
                search_url = f"https://www.amazon.in/10-minutes-delivery/s?k={q.replace(' ', '%20')}"
                products += self._browser_catalog(search_url, f"amazon_now::{station}", "amazon_now", lat, lon)
        for t in self.rotating_terms(1):
            search_url = "https://www.amazon.in/10-minutes-delivery/s?k=" + urllib.parse.quote_plus(t)
            products += self._browser_catalog(search_url, f"amazon_now::{station}", "amazon_now", lat, lon)
        products += self._browser_catalog(self.APP_URL, f"amazon_now::{station}", "amazon_now", lat, lon)
        return products
