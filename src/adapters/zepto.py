"""zepto.py — Zepto adapter.

Zepto rotates its api/v2 paths and walls guessed endpoints behind AWS WAF.
Same browser-intercept strategy: real app in a mobile fingerprint, geolocation
anchored to the station, mirror signed catalog calls.
"""
from __future__ import annotations
import urllib.parse
from .base import Adapter


class ZeptoAdapter(Adapter):
    name = "zepto"

    APP_URL = "https://www.zeptonow.com/"

    def search(self, query, lat=None, lon=None):
        url = "https://www.zeptonow.com/search?q=" + urllib.parse.quote_plus(query)
        return self._browser_catalog(url, "zepto::search", "zepto", lat, lon)

    def crawl(self, station, lat, lon):
        products = []
        for h in [x for x in self.honey if x.get("app") == "zepto"]:
            q = h.get("query", "")
            if q:
                search_url = f"https://www.zeptonow.com/search?q={q.replace(' ', '%20')}"
                products += self._browser_catalog(search_url, f"zepto::{station}", "zepto", lat, lon)
        products += self._browser_catalog(self.APP_URL, f"zepto::{station}", "zepto", lat, lon)
        return products
