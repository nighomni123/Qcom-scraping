"""flipkart.py — Flipkart search via the browser layer.

Flipkart rotates CSS class names constantly; the DOM extractor tries several
known generations and falls back to generic [data-id] anchors.
"""
from __future__ import annotations
import urllib.parse
from .base import Adapter


class FlipkartAdapter(Adapter):
    name = "flipkart"

    def search(self, query, lat=None, lon=None):
        url = "https://www.flipkart.com/search?q=" + urllib.parse.quote_plus(query)
        return self._browser_catalog(url, "flipkart", "flipkart", lat, lon)

    def crawl(self, station=None, lat=None, lon=None):
        return []
