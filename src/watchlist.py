"""
watchlist.py — Demand Radar phase 2: per-darkstore SKU watchlists.

The prober (phase 3) can only detect stock-outs for SKUs it knows about, and
OOS items often VANISH from listings — so the watchlist is the memory that
makes disappearance a signal instead of blindness.

Build strategy per (app, store):
  1. ONE browser session via adapter.deep_sweep(): home harvest -> DOM
     category click-through -> staple search terms. Every intercepted SKU
     arrives stock-stamped and labeled ('home', category, 'q:<term>').
  2. Score: search hits (a searchable SKU is a probeable SKU) > home presence
     > collection spread. High-velocity staples naturally dominate.
  3. Curate: top `watchlist_max_per_store` active, overflow deactivated
     (never deleted — history + reactivation on future sweeps).
"""
from __future__ import annotations

import time

from .geo import Corridor
from .locality import QC_APPS

# High-velocity staples: searchable, fast-moving, OOS-prone. Override via
# config demand.staple_queries (list). These double as the probe set's seed
# queries — a SKU findable by search is cheap to re-probe later.
DEFAULT_STAPLES = [
    "amul milk", "bread", "eggs", "atta", "rice", "toor dal", "maggi",
    "curd", "paneer", "butter", "banana", "onion", "potato", "tomato",
    "cold drinks", "chips", "biscuits", "chocolate", "tea", "coffee",
    "cooking oil", "sugar", "salt", "detergent", "dishwash", "soap",
    "shampoo", "toothpaste", "diapers", "bisleri water",
]


class WatchlistBuilder:
    def __init__(self, cfg, db):
        self.cfg = cfg
        self.db = db
        dem = cfg.get("demand", {}) or {}
        self.categories = int(dem.get("categories_per_store", 6))
        self.staples = list(dem.get("staple_queries") or DEFAULT_STAPLES)
        self.default_cap = int(dem.get("watchlist_max_per_store", 300))

    def _make_adapter(self, app):
        corridor = Corridor(self.cfg.get("geo", {}).get("corridor", []) or [])
        return QC_APPS[app](self.cfg, corridor, [])

    # -- entrypoint --------------------------------------------------------
    def build(self, apps=None, store_filter=None, max_per_store=None, max_queries=None):
        cap = int(max_per_store or self.default_cap)
        staples = self.staples[:max_queries] if max_queries else self.staples
        want_apps = {a.strip().lower() for a in (apps or []) if a.strip()}
        stores = [s for s in self.db.darkstores()
                  if (not want_apps or s[0] in want_apps)
                  and (not store_filter or s[1] == store_filter)]
        if not stores:
            print("[watchlist] no darkstores to build from — run "
                  "`python3 run.py --map-locality` first")
            return
        print(f"[watchlist] building for {len(stores)} store(s) · cap={cap} · "
              f"categories={self.categories} · queries={len(staples)}")
        for app, store_id, label, lat, lon, eta in stores:
            try:
                self._build_store(app, store_id, lat, lon, cap, staples)
            except KeyboardInterrupt:
                print("\n[watchlist] interrupted — partial results saved")
                raise
            time.sleep(2)
        print("[watchlist] per-store totals:")
        for app, sid, total, active in self.db.watchlist_stats():
            print(f"    {app:<10} {sid:<28} active={active}/{total}")

    # -- per store ---------------------------------------------------------
    def _build_store(self, app, store_id, lat, lon, cap, staples):
        adapter = self._make_adapter(app)
        print(f"\n[watchlist] {app} @ store {store_id} ({lat:.4f},{lon:.4f})")
        t0 = time.time()
        products, meta = adapter.deep_sweep(store_id, lat, lon,
                                            categories=self.categories, terms=staples)
        if not products:
            print(f"[watchlist] warn: {app} sweep returned no products "
                  f"({meta.get('error') or 'feed blocked'}) — store skipped")
            return
        agg = {}
        for p in products:
            cols = p.get("collections") or ["home"]
            qhits = sum(1 for c in cols if str(c).startswith("q:"))
            score = 3.0 * qhits + (2.0 if "home" in cols else 0.0) + 0.1 * len(cols)
            agg[p["sku_key"]] = {
                "sku_key": p["sku_key"],
                "name": p.get("name"),
                "collections": cols,
                "price": p.get("price"),
                "in_stock": p.get("in_stock"),
                "score": round(score, 2),
            }
        ranked = sorted(agg.values(), key=lambda r: (-r["score"], str(r["name"])))
        for i, r in enumerate(ranked):
            r["active"] = i < cap
        self.db.upsert_watchlist(app, store_id, ranked)
        self.db.deactivate_watchlist_except(app, store_id, [r["sku_key"] for r in ranked])
        n_oos = sum(1 for r in ranked if r["in_stock"] is False)
        n_stock_known = sum(1 for r in ranked if r["in_stock"] is not None)
        print(f"[watchlist] {app}/{store_id}: {len(ranked)} SKUs seen "
              f"({n_stock_known} stock-stamped, {n_oos} OOS at build) -> "
              f"active={min(cap, len(ranked))} · {time.time() - t0:.0f}s")
        for r in ranked[:8]:
            flag = "OOS" if r["in_stock"] is False else "ok " if r["in_stock"] else "?  "
            print(f"    [{flag}] {str(r['name'])[:52]:<54} ₹{r['price']}  "
                  f"score={r['score']}  via={','.join(r['collections'][:3])}")
        if len(ranked) > 8:
            print(f"    … +{len(ranked) - 8} more")
