"""amazon.py — Amazon.in search via the browser layer.

Amazon renders search results server-side as HTML; the browser layer's DOM
extractor handles it (JSON interception first as a bonus).
"""
from __future__ import annotations
import urllib.parse
from .base import Adapter


class AmazonAdapter(Adapter):
    name = "amazon"

    def search(self, query, lat=None, lon=None):
        url = "https://www.amazon.in/s?k=" + urllib.parse.quote_plus(query)
        return self._browser_catalog(url, "amazon", "amazon", lat, lon)

    def crawl(self, station=None, lat=None, lon=None):
        return []
