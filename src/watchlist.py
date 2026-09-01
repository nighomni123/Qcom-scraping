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
from .store import is_voucher_name

# Unbiased, broad-coverage seed queries: we want to index as many SKUs as
# possible across every department (grocery, paan/tobacco, wellness,
# personal care, household, beauty, baby, ready-to-eat) — not just fast
# grocery staples. Override via config demand.staple_queries (list). These
# double as the probe set's seed queries — a SKU findable by search is
# cheap to re-probe later.
DEFAULT_STAPLES = [
    # Dairy, Breakfast & Staples
    "amul milk", "bread", "eggs", "atta", "rice", "toor dal", "sugar", "salt",
    "curd", "paneer", "butter", "ghee", "poha", "oats", "cornflakes", "honey",
    "besan", "maida", "rava", "wheat", "muesli", "cereal",
    # Produce & Fresh
    "banana", "onion", "potato", "tomato", "apple", "coconut", "lemon",
    "garlic", "ginger", "cucumber", "carrot", "spinach", "mango", "grapes",
    "pomegranate", "capsicum", "cauliflower", "cabbage", "mint", "coriander",
    # Snacks & Beverages
    "maggi", "chips", "biscuits", "chocolate", "cold drinks", "tea", "coffee",
    "juice", "energy drink", "namkeen", "cake", "ice cream", "popcorn",
    "cookie", "soda", "water", "buttermilk", "lassi", "smoothie",
    "kurkure", "nachos", "wafer", "bhelpuri", "sev", "mathri",
    # Paan Corner & Tobacco (high-demand impulse items)
    "cigarette", "lighter", "rolling paper", "mouth freshener", "pan masala",
    "supari", "tobacco", "vape", "gutka", "mint", "gum", "paan", "betel leaf",
    "zarda", "khaini", "bidi", "e-cigarette", "hookah",
    # Personal Care, Wellness & Intimate / Contraceptives
    "condom", "lubricant", "sanitary pad", "tampon", "shampoo", "soap",
    "toothpaste", "toothbrush", "deodorant", "perfume", "face wash",
    "hair oil", "skincare", "razor", "shaving cream", "diapers", "baby food",
    "wet wipes", "body wash", "lotion", "sunscreen", "mask", "hand sanitizer",
    "pregnancy test", "contraceptive pill", "morning after pill",
    "viagra", "fertility test", "vaginal wash", "menstrual cup",
    # Beauty & Grooming
    "lipstick", "nail polish", "foundation", "eyeliner", "perfume",
    "beard oil", "hair gel", "comb", "hair color", "sunscreen",
    # Household & Cleaning
    "cooking oil", "detergent", "dishwash", "floor cleaner", "toilet cleaner",
    "tissue paper", "garbage bags", "mosquito repellent", "bulb", "battery",
    "mop", "sponge", "brush", "air freshener", "phenyl", "bleach",
    "laundry detergent", "stain remover", "insect spray",
    # Instant Foods & Ready-to-Eat
    "pasta", "noodles", "soup", "frozen food", "ketchup", "sauce",
    "mayonnaise", "pizza", "burger", "sandwich", "instant mix", "idli mix",
    "dosa batter", "pav bhaji", "ready meal", "biryani", "pulao",
    # Pet Care
    "cat food", "dog food", "pet treats", "litter", "pet shampoo",
    # Pharma & OTC (non-prescription)
    "crocin", "paracetamol", "antiseptic", "bandage", "ointment",
    "cough syrup", "vitamin", "electral", "ORS", "digestive tablets",
    "pain relief", "antacid", "antihistamine",
    # Electronics & Misc convenience
    "phone charger", "earphones", "cable", "power bank", "usb",
    "candle", "matches", "envelope", "gift wrap", "balloon",
    # Adult & Recreational (to avoid blind spots in demand analytics)
    "beer", "wine", "whisky", "vodka", "gin", "rum", "alcopop",
    "rolling tray", "grinder", "bong", "glass pipe",
]


class WatchlistBuilder:
    def __init__(self, cfg, db):
        self.cfg = cfg
        self.db = db
        dem = cfg.get("demand", {}) or {}
        self.categories = int(dem.get("categories_per_store", 6))
        self.staples = list(dem.get("staple_queries") or DEFAULT_STAPLES)
        self.default_cap = int(dem.get("watchlist_max_per_store", 300))
        # Unbiased harvesting: keep every discovered SKU and never prune the
        # overflow so the catalog is as complete as the app exposes.
        self.unbiased = bool(dem.get("unbiased_harvest", False))
        # Vouchers are not commodities — never track them (AGENTS.md invariant).
        self.exclude_vouchers = bool(dem.get("exclude_vouchers", True))

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
                  "`python3 run.py --map-locality` first", flush=True)
            return
        print(f"[watchlist] building for {len(stores)} store(s) · cap={cap} · "
              f"categories={self.categories} · queries={len(staples)}", flush=True)
        total = len(stores)
        for idx, (app, store_id, label, lat, lon, eta) in enumerate(stores, 1):
            try:
                self._build_store(app, store_id, lat, lon, cap, staples,
                                  index=idx, total=total, label=label)
            except KeyboardInterrupt:
                print("\n[watchlist] interrupted — partial results saved", flush=True)
                raise
            time.sleep(2)
        print("[watchlist] per-store totals:", flush=True)
        for app, sid, total, active in self.db.watchlist_stats():
            print(f"    {app:<10} {sid:<28} active={active}/{total}", flush=True)

    # -- per store ---------------------------------------------------------
    def _build_store(self, app, store_id, lat, lon, cap, staples,
                     index=None, total=None, label=None):
        adapter = self._make_adapter(app)
        idx_s = f" ({index}/{total})" if index else ""
        label_s = f" — {label}" if label else ""
        n_visits = self.categories + len(staples)
        print(f"\n[watchlist]{idx_s} {app} @ store {store_id}{label_s} "
              f"({lat:.4f},{lon:.4f})", flush=True)
        print(f"[watchlist]   queued {self.categories} categories + "
              f"{len(staples)} searches = {n_visits} visits · per-visit "
              f"progress streams below", flush=True)
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
        if self.exclude_vouchers:
            # Gift cards / instant vouchers ride along on category rails
            # (Blinkit's "E-Gift Cards" shelf) and staple searches. Their
            # stock-outs are code-pool replenishments, not demand — drop them
            # BEFORE ranking/upsert so they never enter the watchlist.
            voucher_skus = [k for k, r in agg.items() if is_voucher_name(r["name"])]
            if voucher_skus:
                for k in voucher_skus:
                    del agg[k]
                print(f"[watchlist]   excluded {len(voucher_skus)} voucher/"
                      f"gift-card SKUs (demand.exclude_vouchers)", flush=True)
        ranked = sorted(agg.values(), key=lambda r: (-r["score"], str(r["name"])))
        if self.unbiased:
            # Keep ALL discovered SKUs active so the full catalog is indexed;
            # the prober will still prioritize by score but observations are
            # never lost. No deactivation of overflow.
            for r in ranked:
                r["active"] = True
            self.db.upsert_watchlist(app, store_id, ranked)
        else:
            for i, r in enumerate(ranked):
                r["active"] = i < cap
            self.db.upsert_watchlist(app, store_id, ranked)
            self.db.deactivate_watchlist_except(app, store_id, [r["sku_key"] for r in ranked])
        n_oos = sum(1 for r in ranked if r["in_stock"] is False)
        n_stock_known = sum(1 for r in ranked if r["in_stock"] is not None)
        active_n = sum(1 for r in ranked if r["active"])
        print(f"[watchlist] {app}/{store_id}: {len(ranked)} SKUs seen "
              f"({n_stock_known} stock-stamped, {n_oos} OOS at build) -> "
              f"active={active_n} · {time.time() - t0:.0f}s", flush=True)
        for r in ranked[:8]:
            flag = "OOS" if r["in_stock"] is False else "ok " if r["in_stock"] else "?  "
            print(f"    [{flag}] {str(r['name'])[:52]:<54} ₹{r['price']}  "
                  f"score={r['score']}  via={','.join(r['collections'][:3])}", flush=True)
        if len(ranked) > 8:
            print(f"    … +{len(ranked) - 8} more", flush=True)
