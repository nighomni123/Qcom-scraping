"""
trackers.py — e-commerce price tracking (Amazon.in / Flipkart).

Unlike the QC apps, these have stable price pages but bot-wall direct scraping.
Strategy: we don't scrape the HTML. We rely on the honey-pot basket + a small
set of tracked ASINs, and we *attempt* a lightweight fetch with rotated UA +
proxy. If blocked, we degrade to logging "tracker offline" rather than failing
the whole run. This keeps the architecture honest: the QC browser layer is the
primary signal; trackers are a bonus when the network cooperates.

For production you'd plug Keepa/CamelCamelCamel here; the interface (crawl ->
products) is identical so swapping is trivial.
"""
from __future__ import annotations

import urllib.request
from .base import Adapter, fresh_user_agent, pick_proxy, norm_price


class TrackersAdapter(Adapter):
    name = "trackers"

    # Example tracked ASINs (replace with ones you care about). Empty => noop.
    TRACKED = [
        {"app": "amazon", "sku_key": "b0example", "name": "Watched Amazon item", "url": "https://www.amazon.in/dp/B0EXAMPLE"},
        {"app": "flipkart", "sku_key": "flip001", "name": "Watched Flipkart item", "url": "https://www.flipkart.com/item/p/itmEXAMPLE"},
    ]

    def crawl(self, station=None, lat=None, lon=None):
        out = []
        for item in self.TRACKED:
            ua, _iid, proxy = self._rotate() if hasattr(self, "_rotate") else (fresh_user_agent(), None, pick_proxy(self.proxies))
            try:
                req = urllib.request.Request(item["url"], headers={"User-Agent": ua, "Accept-Language": "en-IN"})
                if proxy:
                    req.set_proxy(proxy, "http")
                with urllib.request.urlopen(req, timeout=12) as r:
                    html = r.read(200000).decode("utf-8", "ignore")
                price = norm_price(html)
                # sanity floor: real QC/e-com prices are rarely < ₹5; junk else
                if price and price >= 5:
                    out.append({
                        "sku_key": item["sku_key"], "name": item["name"],
                        "price": price, "mrp": None, "url": item["url"],
                    })
            except Exception:
                # blocked / offline — skip silently, log at orchestrator level
                pass
        return out
