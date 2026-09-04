"""
watchlist.py — Demand Radar phase 2: per-darkstore SKU watchlists.

The prober (phase 3) can only detect stock-outs for SKUs it knows about, and
OOS items often VANISH from listings — so the watchlist is the memory that
makes disappearance a signal instead of blindness.

Catalog-inventory mode (`--build-watchlist --catalog`, 09-02) instead sweeps
ALL category links (one-hop sub-category discovery via --deep-cats), with NO
search terms and NO category skips, and writes a `catalog_snapshots` row per
SKU. Each run diffs against the previous snapshot: brand-new SKUs -> 'new'
events (limited-time-offering candidates); previously-listed SKUs that are
absent from a FULL sweep -> 'delisted' events + watchlist deactivation (the
discontinued archive). Delisting is SNAPSHOT-DRIVEN ONLY — absence from a
partial sweep (or from the prober's light rounds) is never churn.

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
        # Catalog-inventory mode: upper bound on category visits per full
        # sweep (the queue drains naturally at "all links"; this cap keeps a
        # pathological app from never terminating).
        self.catalog_categories = int(dem.get("catalog_max_categories", 300))

    def _make_adapter(self, app):
        corridor = Corridor(self.cfg.get("geo", {}).get("corridor", []) or [])
        return QC_APPS[app](self.cfg, corridor, [])

    # -- entrypoint --------------------------------------------------------
    def build(self, apps=None, store_filter=None, max_per_store=None,
              max_queries=None, catalog=False, categories_override=None,
              mirror_page_ms=None, tabs=None):
        cap = int(max_per_store or self.default_cap)
        staples = self.staples[:max_queries] if max_queries else self.staples
        if catalog:
            # Catalog-inventory mode: categories are the catalog backbone —
            # ALL discovered links (one-hop --deep-cats BFS), NO search terms,
            # NO category skips (a skipped shelf would fabricate delistings
            # in the diff), and unbiased curation (everything is archived).
            categories = int(categories_override or self.catalog_categories)
            staples = []
            skip_override = []
            deep_cats = True
        else:
            categories = int(categories_override or self.categories)
            skip_override = None
            deep_cats = False
        want_apps = {a.strip().lower() for a in (apps or []) if a.strip()}
        stores = [s for s in self.db.darkstores()
                  if (not want_apps or s[0] in want_apps)
                  and (not store_filter or s[1] == store_filter)]
        if not stores:
            print("[watchlist] no darkstores to build from — run "
                  "`python3 run.py --map-locality` first", flush=True)
            return
        print(f"[watchlist] building for {len(stores)} store(s) · cap={cap} · "
              f"categories={categories} · queries={len(staples)}" +
              (" · CATALOG INVENTORY (snapshot + churn diff)" if catalog else ""),
              flush=True)
        total = len(stores)
        for idx, (app, store_id, label, lat, lon, eta) in enumerate(stores, 1):
            try:
                self._build_store(app, store_id, lat, lon, cap, staples,
                                  index=idx, total=total, label=label,
                                  catalog=catalog, categories=categories,
                                  skip_override=skip_override, deep_cats=deep_cats,
                                  mirror_page_ms=mirror_page_ms, tabs=tabs)
            except KeyboardInterrupt:
                print("\n[watchlist] interrupted — partial results saved", flush=True)
                raise
            time.sleep(2)
        print("[watchlist] per-store totals:", flush=True)
        for app, sid, total, active in self.db.watchlist_stats():
            print(f"    {app:<10} {sid:<28} active={active}/{total}", flush=True)

    # -- per store ---------------------------------------------------------
    def _build_store(self, app, store_id, lat, lon, cap, staples,
                     index=None, total=None, label=None, catalog=False,
                     categories=None, skip_override=None, deep_cats=False,
                     mirror_page_ms=None, tabs=None):
        categories = int(categories or self.categories)
        adapter = self._make_adapter(app)
        idx_s = f" ({index}/{total})" if index else ""
        label_s = f" — {label}" if label else ""
        n_visits = categories + len(staples)
        print(f"\n[watchlist]{idx_s} {app} @ store {store_id}{label_s} "
              f"({lat:.4f},{lon:.4f})", flush=True)
        print(f"[watchlist]   queued {categories} categories + "
              f"{len(staples)} searches = {n_visits} visits · per-visit "
              f"progress streams below", flush=True)
        t0 = time.time()
        products, meta = adapter.deep_sweep(store_id, lat, lon,
                                            categories=categories, terms=staples,
                                            deep_cats=deep_cats,
                                            skip_override=skip_override,
                                            mirror_page_ms=mirror_page_ms, tabs=tabs)
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
        if self.unbiased or catalog:
            # Keep ALL discovered SKUs active so the full catalog is indexed;
            # the prober will still prioritize by score but observations are
            # never lost. No deactivation of overflow. Catalog mode always
            # archives everything it saw — curation caps don't apply.
            for r in ranked:
                r["active"] = True
            self.db.upsert_watchlist(app, store_id, ranked)
        else:
            for i, r in enumerate(ranked):
                r["active"] = i < cap
            self.db.upsert_watchlist(app, store_id, ranked)
            self.db.deactivate_watchlist_except(app, store_id, [r["sku_key"] for r in ranked])
        if catalog:
            self._catalog_snapshot_and_diff(app, store_id, ranked)
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

    # -- catalog inventory: snapshot + churn diff (09-02) -------------------
    # Snapshot pair count collapses to <70% of the previous snapshot ->
    # soft-block / fetch-flake signature, NOT churn (mirrors the prober's
    # suspect-cycle freeze): the snapshot is recorded honestly, but the diff
    # is skipped so a flaky crawl can never mass-archive a store's catalog.
    # 09-04: raised 0.5 -> 0.7 and keyed to (name,price) PAIRS. The 09-04
    # Instamart 1398452 19:52 sweep survived at 56% pair count / 24.9% pair
    # OVERLAP and its diff fabricated ~11.5k delistings (watchlist rows
    # deactivated for products still on sale). 70% keeps honest same-depth
    # sweeps (~95%+ ratio) far clear; the cost of a skip is one diff cycle —
    # the suspect snapshot itself becomes the next baseline, so a consistent
    # follow-up sweep diffs cleanly against it.
    SUSPECT_SNAPSHOT_RATIO = 0.7

    def _catalog_snapshot_and_diff(self, app, store_id, ranked):
        now = time.time()
        prev_ts, prev = self.db.catalog_prev_snapshot(
            app, store_id, before_ts=now, with_prices=True)
        n = self.db.record_catalog_snapshot(app, store_id, now, ranked)
        if prev_ts is None:
            print(f"[catalog] {app}/{store_id}: BASELINE snapshot — {n} SKUs "
                  f"archived (first snapshot; churn diffing starts next run)",
                  flush=True)
            return
        cur = {r["sku_key"]: r for r in ranked}
        # Pair reconciliation (09-04): Instamart shelves sometimes omit every
        # id field, so sku_key falls back to a name-slug there and rotates
        # slug->id between sweeps — raw key diffs read that as churn. Reconcile
        # on exact (name, price) pairs: a "new"/"delisted" key whose pair
        # exists on the other side is a re-key, not churn (excluded from
        # events + watchlist deactivation; logged as reconciled below).
        prev_pairs = set(prev.values())            # {(name, price)}
        cur_pairs = {(r.get("name") or "", r.get("price")) for r in ranked}
        age_h = (now - prev_ts) / 3600.0
        # Suspect guard on PAIRS: a depth-collapsed sweep also drops pairs
        # wholesale, and pairs are immune to key rotation — a <50% pair drop
        # is always a crawl flake, never churn (raw keys exaggerated it).
        if len(cur_pairs) < self.SUSPECT_SNAPSHOT_RATIO * len(prev_pairs):
            print(f"[catalog] {app}/{store_id}: SUSPECT snapshot — {len(cur_pairs)} "
                  f"distinct (name,price) pairs vs {len(prev_pairs)} previously "
                  f"(<{int(self.SUSPECT_SNAPSHOT_RATIO * 100)}%): archived, diff "
                  f"SKIPPED (mass absence = crawl flake, not churn)", flush=True)
            return
        new_skus = [k for k in cur if k not in prev]
        delisted_skus = [k for k in prev if k not in cur]
        # Re-keyed products: drop from churn lists (they are NOT events).
        reconciled_new = {k for k in new_skus if (cur[k].get("name") or "", cur[k].get("price")) in prev_pairs}
        reconciled_del = {k for k in delisted_skus if prev[k] in cur_pairs}
        new_skus = [k for k in new_skus if k not in reconciled_new]
        delisted_skus = [k for k in delisted_skus if k not in reconciled_del]
        n_rekey = len(reconciled_new) + len(reconciled_del)
        events = []
        for k in new_skus:
            r = cur[k]
            events.append({"sku_key": k, "kind": "new", "name": r.get("name"),
                           "price": r.get("price"), "snapshot_ts": now,
                           "prev_snapshot_ts": prev_ts,
                           "detail": ",".join((r.get("collections") or [])[:3])})
        for k in delisted_skus:
            events.append({"sku_key": k, "kind": "delisted", "name": prev[k][0],
                           "price": None, "snapshot_ts": now,
                           "prev_snapshot_ts": prev_ts,
                           "detail": "absent from full-catalog sweep"})
        if events:
            self.db.record_catalog_events(app, store_id, events)
        if delisted_skus:
            self.db.deactivate_watchlist_skus(app, store_id, delisted_skus)
        print(f"[catalog] {app}/{store_id}: snapshot {n} SKUs vs "
              f"{len(prev)} {age_h:.1f}h ago -> NEW={len(new_skus)} "
              f"DELISTED={len(delisted_skus)}"
              + (f" · rekeyed (not churn): {n_rekey}" if n_rekey else ""),
              flush=True)
        for k in new_skus[:5]:
            r = cur[k]
            print(f"    [new] {str(r.get('name'))[:52]:<54} ₹{r.get('price')}  "
                  f"via={','.join((r.get('collections') or [])[:2])}", flush=True)
        for k in delisted_skus[:5]:
            print(f"    [delisted] {str(prev[k][0])[:56]}  (was listed "
                  f"{age_h:.1f}h ago)", flush=True)
        if len(new_skus) > 5 or len(delisted_skus) > 5 or n_rekey > 5:
            print(f"    … full churn log: catalog_events table / --catalog-report",
                  flush=True)


if __name__ == "__main__":
    # Offline self-test: pair reconciliation + pair-based suspect guard,
    # replaying the observed 09-04 Instamart 1398452 slug->id re-key pattern.
    # Pure logic — a fake DB records what the diff WOULD write; no network.
    class FakeDB:
        def __init__(self, prev):
            self.prev = prev
            self.snapshots = []
            self.events = []
            self.deactivated = []

        def catalog_prev_snapshot(self, app, store_id, before_ts=None,
                                  with_prices=False):
            assert with_prices, "diff must request prices for pair reconciliation"
            ts = 1000.0
            return (ts, {k: (n, p) for k, (n, p) in self.prev.items()}) \
                if with_prices else (ts, {k: n for k, (n, p) in self.prev.items()})

        def record_catalog_snapshot(self, app, store_id, ts, rows):
            self.snapshots = rows
            return len(rows)

        def record_catalog_events(self, app, store_id, events):
            self.events = events
            return len(events)

        def deactivate_watchlist_skus(self, app, store_id, keys):
            self.deactivated = keys
            return len(keys)

    def row(k, name, price):
        return {"sku_key": k, "name": name, "price": price,
                "in_stock": True, "collections": ["shelf"]}

    # Case 1 (the observed 09-04 Instamart pattern): Kurkure re-keys
    # slug->id between sweeps (same name+price), Amul is stable, Bingo is
    # genuinely delisted, OnePlus is genuinely new.
    fake = FakeDB(prev={"slugkey": ("Kurkure Masala Munch", 20.0),
                        "keep1":    ("Amul Gold Ice Cream", 120.0),
                        "gonekey":  ("Bingo Mad Angles", 52.0)})
    wb = WatchlistBuilder({"demand": {}}, fake)
    cur = [row("01jjdlrzid", "Kurkure Masala Munch", 20.0),  # re-keyed in
           row("keep1", "Amul Gold Ice Cream", 120.0),      # stable
           row("brandnew", "OnePlus Nord 5", 34999.0)]       # genuinely new
    wb._catalog_snapshot_and_diff("instamart", "s1", cur)
    kinds = {(e["sku_key"], e["kind"]) for e in fake.events}
    assert ("brandnew", "new") in kinds, "genuinely new must be an event"
    assert ("gonekey", "delisted") in kinds, "vanished pair must be an event"
    assert not any(k == "01jjdlrzid" for k, _ in kinds), "re-keyed must NOT be an event"
    assert fake.deactivated == ["gonekey"], "only real delistings deactivate"
    assert len(fake.events) == 2, f"expected exactly 2 events, got {len(fake.events)}"

    # Case 2: suspect guard on pairs — half the catalog "vanishes" by key
    # AND by pair => crawl flake => diff skipped, zero events.
    big_prev = {f"k{i}": (f"Item {i}", float(i)) for i in range(100)}
    fake2 = FakeDB(prev=big_prev)
    wb.db = fake2
    flake = [row(f"k{i}", f"Item {i}", float(i)) for i in range(40)]  # 40% pairs survive
    wb._catalog_snapshot_and_diff("instamart", "s2", flake)
    assert fake2.events == [], "collapsed sweep must record snapshot, zero events"
    assert fake2.deactivated == [], "collapsed sweep must not deactivate"

    # Case 3: healthy sweep — 100% pair survival, one re-key excluded.
    fake3 = FakeDB(prev={f"k{i}": (f"Item {i}", float(i)) for i in range(50)})
    wb.db = fake3
    healthy = [row(f"k{i}", f"Item {i}", float(i)) for i in range(49)]
    healthy.append(row("newkey", "Item 49", 49.0))  # re-keyed id for last item
    wb._catalog_snapshot_and_diff("instamart", "s3", healthy)
    assert fake3.events == [], "re-key-only sweep = zero churn events"
    assert fake3.deactivated == []

    print("[watchlist] self-test OK: pair reconciliation + suspect guard "
          "(3 cases, 0 false churn events)")
