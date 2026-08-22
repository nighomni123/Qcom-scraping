"""
demo.py — simulated source adapter for end-to-end demonstration.

The live Blinkit/Instamart/Zepto adapters need Playwright + a real browser and,
in many networks, a residential proxy to defeat the bot walls. To PROVE the
whole pipeline works (geo -> adapter -> detector -> store -> alert) without
those dependencies, this adapter emits a realistic catalog per station — and
deliberately injects a glitched price on one SKU at one store, so you can see a
real alert fire end-to-end.

This is also what `python3 run.py --demo` uses. It is NOT a mock of the logic;
it feeds the SAME detector, store, and alert code paths the live adapters use.
"""
from __future__ import annotations

from .base import Adapter

# A believable catalog. Prices are per-SKU "normal" values; the demo injector
# below warps one of them at one station to simulate a pricing glitch.
CATALOG = [
    {"sku_key": "amulmilk1l",   "name": "Amul Milk 1L",          "price": 66.0, "mrp": 70.0},
    {"sku_key": "lays90g",      "name": "Lay's India Magic 90g", "price": 20.0, "mrp": 30.0},
    {"sku_key": "maggi12pk",    "name": "Maggi 12-Pack",         "price": 168.0,"mrp": 240.0},
    {"sku_key": "amulgold1l",   "name": "Amul Gold 1L",          "price": 72.0, "mrp": 75.0},
    {"sku_key": "kitkatmocha",  "name": "KitKat Mocha 8s",       "price": 99.0, "mrp": 130.0},
    {"sku_key": "surfexcel1kg", "name": "Surf Excel 1kg",        "price": 110.0,"mrp": 150.0},
]

# (app, station, sku_key, glitched_price) — the injected glitch for the demo.
GLITCH = ("blinkit", "Andheri", "amulmilk1l", 29.0)


class DemoAdapter(Adapter):
    name = "demo"
    # Which app's honey-pot basket + alert labels this adapter maps to.
    honey_app = "blinkit"

    def __init__(self, cfg, corridor, honey=None):
        super().__init__(cfg, corridor, honey)
        self.app = "blinkit"

    def crawl(self, station, lat, lon):
        out = []
        for item in CATALOG:
            price = item["price"]
            # Inject the glitch exactly once, at the configured station.
            if self.app == GLITCH[0] and station == GLITCH[1] and item["sku_key"] == GLITCH[2]:
                price = GLITCH[3]
            out.append({
                "sku_key": item["sku_key"],
                "name": item["name"],
                "price": price,
                "mrp": item["mrp"],
                "url": f"https://blinkit.com/p/{item['sku_key']}",
            })
        return out
