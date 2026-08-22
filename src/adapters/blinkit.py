"""blinkit.py — Blinkit adapter.

Blinkit hard-blocks non-app traffic (we saw 403 to guessed APIs). So we use the
browser-intercept path: load blinkit.com in a mobile WebView fingerprint, set
geolocation to the station coordinate (resolves the nearest dark store), and
mirror the signed catalog calls. Honey-pot SKUs are force-searched each cycle.
"""
from __future__ import annotations
import urllib.parse
from .base import Adapter


class BlinkitAdapter(Adapter):
    name = "blinkit"

    APP_URL = "https://blinkit.com/"

    def search(self, query, lat=None, lon=None):
        url = "https://blinkit.com/s/?q=" + urllib.parse.quote_plus(query)
        return self._browser_catalog(url, "blinkit::search", "blinkit", lat, lon)

    def crawl(self, station, lat, lon):
        products = []
        # Honey-pot probes (instant glitch canaries for this app).
        for h in [x for x in self.honey if x.get("app") == "blinkit"]:
            q = h.get("query", "")
            if q:
                search_url = f"https://blinkit.com/s/?q={q.replace(' ', '%20')}"
                products += self._browser_catalog(search_url, f"blinkit::{station}", "blinkit", lat, lon)
        # General catalog sweep at this location.
        products += self._browser_catalog(self.APP_URL, f"blinkit::{station}", "blinkit", lat, lon)
        return products
