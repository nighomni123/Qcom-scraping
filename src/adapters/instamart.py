"""instamart.py — Swiggy Instamart adapter.

Instamart returns 202 + empty body to plain requests (soft JS challenge) and
sits behind AWS WAF for headless traffic. The browser solves what it can; we
anchor geolocation per station so each dark store resolves separately.
"""
from __future__ import annotations
import urllib.parse
from .base import Adapter


class InstamartAdapter(Adapter):
    name = "instamart"

    APP_URL = "https://instamart.in"
    # Fresh sessions sit behind the address/onboarding sheet (cards:[] until
    # confirmed; search API 403s pre-onboard). pw_catalog.js drives the app's
    # OWN location CTAs when a session is stuck at zero products. Terms add
    # breadth once the session is through the gate.
    PROBE_TERMS = ("bread", "chips")

    def search(self, query, lat=None, lon=None):
        url = "https://instamart.in/search?query=" + urllib.parse.quote_plus(query)
        return self._browser_catalog(url, "instamart::search", "instamart", lat, lon)

    def crawl(self, station, lat, lon):
        products = []
        for h in [x for x in self.honey if x.get("app") == "instamart"]:
            q = h.get("query", "")
            if q:
                search_url = f"https://instamart.in/search?q={q.replace(' ', '%20')}"
                products += self._browser_catalog(search_url, f"instamart::{station}", "instamart", lat, lon)
        products += self._browser_catalog(self.APP_URL, f"instamart::{station}", "instamart", lat, lon)
        return products
