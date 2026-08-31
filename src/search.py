"""
search.py — cross-platform product price search.

Given a free-text product query, fans out SIMULTANEOUSLY (thread pool) to every
enabled platform — Blinkit, Zepto, Instamart, Amazon, Flipkart — fuzzy-matches
the results against the query, applies delivery fees + the best applicable
code/offer per platform (codes.yaml), and returns a ranked cheapest-first list.
"""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .pricing import load_pricing_kb, effective_price


def _tokens(s):
    return [t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if t]


def match_score(query, name, context=None):
    """Fraction of query tokens present in the product name.

    `context` (optional) is extra matching text such as the collection/shelf
    label a product was harvested under (product dicts carry it as
    `collections`). Blinkit's text search serves no tobacco — the Paan Shop
    cigarette shelf is harvested via direct URL instead (adapters/blinkit.py),
    so a shelf product like "Marlboro Advance" only matches the generic query
    "cigarette" through its `pre:Cigarettes` shelf label.
    """
    q = _tokens(query)
    if not q:
        return 0.0
    n = " ".join(_tokens(name))
    if context:
        n += " " + " ".join(_tokens(" ".join(str(c) for c in context)))
    hit = sum(1 for t in q if t in n)
    return hit / len(q)


class SearchEngine:
    def __init__(self, cfg):
        self.cfg = cfg
        scfg = cfg.get("search", {})
        self.station = scfg.get("station", "Andheri")
        self.per_platform = int(scfg.get("per_platform_limit", 3))
        self.min_score = float(scfg.get("min_match_score", 0.5))
        self.platforms = scfg.get("platforms",
                                  ["blinkit", "zepto, instamart".split(",")[0], "instamart",
                                   "amazon", "flipkart"])
        # normalize
        if isinstance(self.platforms, str):
            self.platforms = [p.strip() for p in self.platforms.split(",")]
        self.fees, self.offers = load_pricing_kb(cfg.get("codes_file", "codes.yaml"))
        self._adapters = {}
        geo = next((s for s in cfg.get("geo", {}).get("corridor", [])
                    if s["station"] == self.station),
                   {"station": self.station, "lat": 19.119, "lon": 72.846})
        self.lat, self.lon = geo["lat"], geo["lon"]
        from .adapters.blinkit import BlinkitAdapter
        from .adapters.zepto import ZeptoAdapter
        from .adapters.instamart import InstamartAdapter
        from .adapters.amazon import AmazonAdapter
        from .adapters.flipkart import FlipkartAdapter
        from .adapters.bigbasket import BigbasketAdapter
        from .adapters.jiomart import JiomartAdapter
        makers = {"blinkit": BlinkitAdapter, "zepto": ZeptoAdapter,
                  "instamart": InstamartAdapter, "amazon": AmazonAdapter,
                  "flipkart": FlipkartAdapter, "bigbasket": BigbasketAdapter,
                  "jiomart": JiomartAdapter}
        honey = []  # search mode doesn't need canaries
        for p in self.platforms:
            if p in makers:
                self._adapters[p] = makers[p](cfg, None, honey)

    def _search_one(self, platform, query):
        ad = self._adapters[platform]
        try:
            return platform, ad.search(query, self.lat, self.lon) or []
        except Exception as ex:
            return platform, []

    def search(self, query, source=None):
        """Returns dict: {results: [...ranked...], platforms_tried, timings}."""
        t0 = time.time()
        raw, timings = {}, {}
        with ThreadPoolExecutor(max_workers=max(len(self._adapters), 1)) as ex:
            futs = {ex.submit(self._search_one, p, query): p for p in self._adapters}
            for fut in as_completed(futs):
                p = futs[fut]
                ts = time.time()
                try:
                    plat, prods = fut.result()
                except Exception:
                    plat, prods = p, []
                raw[plat] = prods or []
                timings[plat] = round(time.time() - ts, 1)

        results = []
        seen_names = set()
        for platform, prods in raw.items():
            scored = []
            for pr in prods:
                s = match_score(query, pr.get("name"), pr.get("collections"))
                if s >= self.min_score and pr.get("price"):
                    scored.append((s, pr))
            scored.sort(key=lambda x: (-x[0], x[1]["price"]))
            for s, pr in scored[:self.per_platform]:
                eff = effective_price(platform, pr["price"], self.fees, self.offers)
                key = (platform, re.sub(r"\W+", "", (pr.get("name") or "").lower())[:50])
                if key in seen_names:
                    continue
                seen_names.add(key)
                results.append({
                    "platform": platform,
                    "name": pr.get("name"),
                    "match": round(s, 2),
                    "mrp": pr.get("mrp"),
                    "url": pr.get("url") or "",
                    **eff,
                })
        results.sort(key=lambda r: r["effective"])
        # tag categories + persist for future analysis
        from .categories import categorize
        for r in results:
            r["category"] = categorize(r.get("name"))
        elapsed = round(time.time() - t0, 1)
        search_id = None
        try:
            from .store import Store
            if not hasattr(self, "_store") or self._store is None:
                self._store = Store(self.cfg.get("db", "deals.db"))
            search_id = self._store.save_search(query, source, elapsed, results)
        except Exception:
            pass
        return {
            "query": query,
            "results": results,
            "counts": {p: len(v) for p, v in raw.items()},
            "timings": timings,
            "elapsed": elapsed,
            "search_id": search_id,
        }


def format_reply(res):
    """Human-readable reply for Telegram/CLI."""
    q = res["query"]
    lines = [f"🔎 *“{q}”* — cheapest across platforms", ""]
    if not res["results"]:
        lines.append("No matches found. Try a simpler brand name (e.g. 'amul milk').")
    for i, r in enumerate(res["results"][:10], 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
        price_bits = [f"₹{r['listed']:.0f}"]
        if r.get("mrp"):
            price_bits.append(f"(MRP ₹{r['mrp']:.0f})")
        line = f"{medal} *{r['platform'].upper()}* — {r['name'][:60]}\n     {' '.join(price_bits)}"
        extras = []
        if r["discount"]:
            extras.append(f"code {r['code']} −₹{r['discount']:.0f}")
        if r["delivery"]:
            extras.append(f"+₹{r['delivery']:.0f} delivery")
        else:
            extras.append("free delivery")
        line += "\n     → *₹%.0f effective*  (%s)" % (r["effective"], ", ".join(extras))
        if r.get("note"):
            line += f"\n     ⚠️ {r['note']}"
        lines.append(line)
    counts = " · ".join(f"{p}:{c}" for p, c in res["counts"].items())
    lines.append("")
    lines.append(f"_scanned {len(res['counts'])} platforms in {res['elapsed']}s ({counts})_")
    text = "\n".join(lines)
    return text[:4000]  # telegram cap is 4096
