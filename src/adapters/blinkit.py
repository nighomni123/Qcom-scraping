"""blinkit.py — Blinkit adapter.

Blinkit hard-blocks non-app traffic (we saw 403 to guessed APIs). So we use the
browser-intercept path: load blinkit.com in a mobile WebView fingerprint, set
geolocation to the station coordinate (resolves the nearest dark store), and
mirror the signed catalog calls. Honey-pot SKUs are force-searched each cycle.
"""
from __future__ import annotations
import re
import urllib.parse
from .base import Adapter


class BlinkitAdapter(Adapter):
    name = "blinkit"

    APP_URL = "https://blinkit.com/"
    # Home feed serves dairy-first carousels; one-shot probes add non-dairy
    # search terms in-session so indexing sees real catalog breadth.
    # Home feed is dairy-first; broaden to non-food / paan-shop SKUs so
    # --store-inventory captures convenience + tobacco categories too.
    PROBE_TERMS = ("chips", "shampoo", "atta", "paan", "cigarette",
                   "gutkha", "pan masala", "tobacco", "condom", "mukhwas")

    # Blinkit's TEXT SEARCH serves no tobacco: it is server-side curated to
    # smoking accessories (lighters etc.) — verified 08-30 across 13 darkstores
    # (668 q:cigarettes captures, zero tobacco names) AND with sessions warmed
    # by the shelf itself (search still returns only lighters). The catalog
    # itself IS reachable without any gate: direct shelf/product URLs
    # (/cn///cid/229/1948, /prn/...) serve the full cigarette assortment — the
    # "appropriate age / not near schools" interstitial guards ONLY the
    # banner-click path in the UI. So tobacco queries pre-visit the cigarette
    # shelf in the same browser session (pw_catalog.js --pre) and merge it with
    # the text-search harvest; search.match_score counts the `pre:Cigarettes`
    # shelf label toward the query match ("cigarette" → "Marlboro Advance").
    TOBACCO_RE = re.compile(
        r"cigar|cigarette|marlboro|marlbro|gold flake|benson|hedges|bidi|"
        r"gutkha|pan masala|tobacco|smok", re.I)
    CIGARETTE_SHELF = ("https://blinkit.com/cn///cid/229/1948", "Cigarettes")

    def search(self, query, lat=None, lon=None):
        url = "https://blinkit.com/s/?q=" + urllib.parse.quote_plus(query)
        pre = [self.CIGARETTE_SHELF] if self.TOBACCO_RE.search(query or "") else None
        return self._browser_catalog(url, "blinkit::search", "blinkit", lat, lon, pre=pre)

    def crawl(self, station, lat, lon):
        products = []
        # Honey-pot probes (instant glitch canaries for this app).
        for h in [x for x in self.honey if x.get("app") == "blinkit"]:
            q = h.get("query", "")
            if q:
                search_url = f"https://blinkit.com/s/?q={q.replace(' ', '%20')}"
                products += self._browser_catalog(search_url, f"blinkit::{station}", "blinkit", lat, lon)
        # Rotating coverage terms (schedule.crawl_terms): diversify price_obs
        # beyond the dairy-first home carousel + honey canaries.
        for t in self.rotating_terms(1):
            search_url = "https://blinkit.com/s/?q=" + urllib.parse.quote_plus(t)
            products += self._browser_catalog(search_url, f"blinkit::{station}", "blinkit", lat, lon)
        # General catalog sweep at this location.
        products += self._browser_catalog(self.APP_URL, f"blinkit::{station}", "blinkit", lat, lon)
        return products
