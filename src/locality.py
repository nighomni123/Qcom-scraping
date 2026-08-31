"""
locality.py — Demand Radar phase 1: locality anchors + darkstore discovery.

A locality (e.g. Andheri West) is served by SEVERAL quick-commerce darkstores.
Stock state and ETA are PER STORE, so before any demand analysis we need the
store map: which stores serve the locality, where their catchments sit, and a
rotation pool of anchor coordinates per store.

Method
------
1. Ordered anchors: landmark seeds first (real neighborhood centers), then a
   bbox grid sorted center-out (dense core first, boundaries later).
2. Per app: probe anchors through the REAL app in headless Chromium
   (browser-intercept) and read the resolved darkstore identity from the
   intercepted API traffic (generic store-id candidates — the app is the API
   documentation, we never guess endpoint shapes).
3. Cluster anchor points by resolved store id; persist representatives to the
   `darkstores` table; export the full mapping (incl. rotation pools) to
   exports/locality_<name>.json.

Address-randomisation rule (deliberate change vs naive idea): stock is per
STORE, not per address — probing one store from five addresses in one run
returns identical data and multiplies the bot fingerprint. The grid exists to
DISCOVER catchment boundaries once; later probing hits each store once per
cycle and rotates which anchor represents it.
"""
from __future__ import annotations

import json
import math
import os
import re
import time

from .geo import Corridor
from .adapters.blinkit import BlinkitAdapter
from .adapters.instamart import InstamartAdapter
from .adapters.zepto import ZeptoAdapter
from .adapters.bigbasket import BigbasketAdapter
from .adapters.jiomart import JiomartAdapter
from .adapters.amazon_now import AmazonNowAdapter
from .adapters.dmart import DmartAdapter

QC_APPS = {"blinkit": BlinkitAdapter, "instamart": InstamartAdapter,
           "zepto": ZeptoAdapter, "bigbasket": BigbasketAdapter,
           "jiomart": JiomartAdapter, "amazon_now": AmazonNowAdapter,
           "dmart": DmartAdapter}

# Landmark seeds for supported localities. Coords from OSM/Nominatim (marked
# "osm") or ±200m approximations (marked "approx") — good enough to resolve a
# darkstore whose catchment is much larger than this error. Add your own via
# config: demand.locality.landmarks accepts {name, lat, lon} entries too.
PRESET_LANDMARKS = {
    "andheri west": {
        # osm
        "dn nagar":           (19.12196, 72.83086),
        "four bungalows":     (19.12879, 72.82555),
        "kokilaben hospital": (19.13129, 72.82463),
        "lokhandwala":        (19.14445, 72.82409),
        "infiniti mall":      (19.14128, 72.83095),
        # approx
        "andheri west station": (19.1197, 72.8446),
        "seven bungalows":      (19.1330, 72.8244),
        "versova":              (19.1345, 72.8120),
        "yari road":            (19.1320, 72.8170),
    },
}

# Ranking mirrors tools/pw_catalog.js STORE_KEY_PRIORITY.
_KEY_PRIORITY = ["store_id", "storeid", "dark_store_id", "darkstoreid",
                 "merchant_id", "merchantid",
                 "warehouse_id", "warehouseid", "wh_id", "whid",
                 "vendor_id", "vendorid", "dc_id", "store", "warehouse",
                 "pod_id", "podid"]


def pick_store(candidates):
    """
    Choose the most plausible darkstore identity from generic candidates.
    Returns {store_id, label, key, count} or None.
    """
    ranked = []
    for c in candidates or []:
        v = str(c.get("value", "")).strip()
        if not v or v.lower() in ("true", "false", "null", "none", "0"):
            continue
        if re.fullmatch(r"-?\d+\.\d+", v):      # coordinate-like float
            continue
        key = str(c.get("key", "")).lower()
        pri = _KEY_PRIORITY.index(key) if key in _KEY_PRIORITY else len(_KEY_PRIORITY)
        ranked.append((-int(c.get("count", 1)), pri, len(v), c))
    if not ranked:
        return None
    ranked.sort()
    best = ranked[0][3]
    return {"store_id": str(best.get("value")),
            "label": best.get("label"),
            "key": best.get("key"),
            "count": best.get("count", 1)}


def build_anchors(loc_cfg):
    """
    Ordered probe points for a locality config:
    landmarks first, then bbox grid center-out. Deduped at ~110 m resolution.
    Returns [{label, lat, lon, kind}].
    """
    name = (loc_cfg.get("name") or "locality").lower().strip()
    presets = {}
    for pname, marks in PRESET_LANDMARKS.items():
        if pname in name or name in pname:
            presets.update(marks)

    anchors, seen_cells = [], set()

    def add(label, lat, lon, kind):
        cell = (round(lat / 0.001, 0), round(lon / 0.001, 0))  # ~110 m cells
        if cell in seen_cells:
            return
        seen_cells.add(cell)
        anchors.append({"label": label, "lat": round(lat, 5), "lon": round(lon, 5), "kind": kind})

    n_landmarks = 0
    for lm in loc_cfg.get("landmarks", []) or []:
        if isinstance(lm, dict) and lm.get("lat") and lm.get("lon"):
            add(str(lm.get("name", "lm")), float(lm["lat"]), float(lm["lon"]), "landmark")
            n_landmarks += 1
            continue
        key = str(lm).lower().strip()
        hit = None
        for pk, pv in presets.items():
            if pk in key or key in pk:
                hit = pv
                break
        if hit:
            add(str(lm), hit[0], hit[1], "landmark")
            n_landmarks += 1
        else:
            print(f"[locality] warn: no preset coords for landmark “{lm}” — skipped "
                  f"(pass {{name,lat,lon}} in config instead)")

    bbox = loc_cfg.get("bbox") or {}
    step_m = float(loc_cfg.get("grid_step_m", 700))
    try:
        lat0, lat1 = float(bbox["min_lat"]), float(bbox["max_lat"])
        lon0, lon1 = float(bbox["min_lon"]), float(bbox["max_lon"])
    except (KeyError, TypeError):
        print("[locality] warn: no usable bbox — landmarks only")
        return anchors

    mid_lat = (lat0 + lat1) / 2.0
    dlat = step_m / 111_320.0
    dlon = step_m / (111_320.0 * math.cos(math.radians(mid_lat)))
    grid = []
    r = 0
    lat = lat0
    while lat <= lat1 + 1e-9:
        c = 0
        lon = lon0
        while lon <= lon1 + 1e-9:
            grid.append((f"grid r{r}c{c}", lat, lon))
            lon += dlon
            c += 1
        lat += dlat
        r += 1
    clat, clon = mid_lat, (lon0 + lon1) / 2.0
    grid.sort(key=lambda g: (g[1] - clat) ** 2 + (g[2] - clon) ** 2)  # center-out
    for label, lat, lon in grid:
        add(label, lat, lon, "grid")

    return anchors


class LocalityMapper:
    def __init__(self, cfg, db):
        self.cfg = cfg
        self.db = db
        dem = cfg.get("demand", {}) or {}
        self.loc_cfg = dem.get("locality", {}) or {}
        self.min_points = int(dem.get("min_points_before_early_stop", 6))
        self.saturation = int(dem.get("saturation_stop", 4))

    # -- adapters ---------------------------------------------------------
    def enabled_apps(self, apps=None):
        ad = self.cfg.get("adapters", {}) or {}
        avail = [a for a in QC_APPS if ad.get(a, {}).get("enabled", True)]
        if apps:
            want = {a.strip().lower() for a in apps if a.strip()}
            avail = [a for a in avail if a in want]
        return avail

    def _make_adapter(self, app):
        corridor = Corridor(self.cfg.get("geo", {}).get("corridor", []) or [])
        return QC_APPS[app](self.cfg, corridor, [])

    # -- discovery --------------------------------------------------------
    def discover_for_app(self, adapter, anchors, capture_products=False):
        """Probe anchors sequentially; cluster by resolved store id.

        capture_products=True (inventory mode) also persists each probe's
        products into the mapper's DB: price_obs (name/price/url, auto-
        categorized) + stock_obs (stock/price/mrp/eta, source='inventory').
        Only probes that resolved a store are captured, so every row stays
        store-attributed. Default False keeps --map-locality store-only.
        """
        seen, history = {}, []
        last_new = -1
        for i, pt in enumerate(anchors):
            t0 = time.time()
            try:
                products, meta = adapter.probe_point(pt["label"], pt["lat"], pt["lon"])
            except KeyboardInterrupt:
                raise
            except Exception as ex:               # never kill the sweep for one bad point
                products, meta = [], {"error": str(ex)[:120]}
            pick = pick_store(meta.get("store_candidates"))
            eta = meta.get("eta_min")
            rec = dict(pt)
            rec.update({
                "resolved_store": pick["store_id"] if pick else None,
                "eta_min": eta,
                "products": len(products),
                "error": meta.get("error"),
                "sec": round(time.time() - t0, 1),
            })
            history.append(rec)
            print(f"  [{adapter.name}] {pt['label']:<30} -> store="
                  f"{pick['store_id'] if pick else '—'}  eta={eta}  "
                  f"products={len(products)}  ({rec['sec']}s)")
            if pick:
                is_new = pick["store_id"] not in seen
                entry = seen.setdefault(pick["store_id"], {
                    "app": adapter.name,
                    "store_id": pick["store_id"],
                    "label": pick.get("label") or f"{adapter.name} store {pick['store_id']}",
                    "points": [],
                    "eta_min": None,
                })
                entry["points"].append({"label": pt["label"], "lat": pt["lat"], "lon": pt["lon"]})
                if eta and (entry["eta_min"] is None or eta < entry["eta_min"]):
                    entry["eta_min"] = eta
                rep = entry["points"][0]
                self.db.upsert_darkstore(adapter.name, pick["store_id"], entry["label"],
                                         rep["lat"], rep["lon"], entry["eta_min"])
                if is_new:
                    last_new = i
            if capture_products and pick and products:
                # Inventory mode: persist what this probe saw, store-attributed.
                # stock_obs: stock/price/mrp/eta (source='inventory');
                # price_obs: name/price/mrp/url (auto-categorized).
                try:
                    self.db.record_stock_obs(
                        adapter.name, pick["store_id"],
                        [{"sku_key": p.get("sku_key"), "in_stock": p.get("in_stock"),
                          "price": p.get("price"), "mrp": p.get("mrp"),
                          "source": "inventory"} for p in products],
                        eta_min=eta)
                    for p in products:
                        self.db.record(adapter.name, pick["store_id"],
                                       p.get("sku_key"), p.get("name"),
                                       p.get("price"), p.get("mrp"),
                                       p.get("url", ""))
                except Exception as ex:
                    print(f"  [{adapter.name}] warn: product capture failed: "
                          f"{str(ex)[:100]}")
            if (i + 1) >= self.min_points and last_new >= 0 and (i - last_new) >= self.saturation:
                print(f"  [{adapter.name}] no new stores in {self.saturation} consecutive "
                      f"probes — stopping early ({i + 1}/{len(anchors)} anchors probed)")
                break
            time.sleep(2 + 3 * (i % 2))          # light politeness gap between loads
        return seen, history

    # -- entrypoint --------------------------------------------------------
    def map_locality(self, apps=None, max_points=None, capture_products=False):
        loc_name = self.loc_cfg.get("name", "locality")
        anchors = build_anchors(self.loc_cfg)
        if max_points:
            anchors = anchors[:max_points]
        lm = sum(1 for a in anchors if a["kind"] == "landmark")
        print(f"[locality] {loc_name}: {len(anchors)} anchors ({lm} landmarks, "
              f"{len(anchors) - lm} grid) · apps={self.enabled_apps(apps)}")
        result = {
            "locality": loc_name,
            "generated_ts": time.time(),
            "anchor_count": len(anchors),
            "anchors": anchors,
            "apps": {},
        }
        try:
            for app in self.enabled_apps(apps):
                adapter = self._make_adapter(app)
                print(f"[locality] mapping {app} …")
                seen, history = self.discover_for_app(adapter, anchors,
                                                      capture_products=capture_products)
                stores = []
                for s in seen.values():
                    stores.append({k: s[k] for k in ("store_id", "label", "points", "eta_min")})
                stores.sort(key=lambda s: -len(s["points"]))
                result["apps"][app] = {"stores": stores, "history": history}
                print(f"[locality] {app}: {len(stores)} distinct darkstore(s) — " +
                      ", ".join(f"{s['store_id']}({len(s['points'])} pts)" for s in stores))
                if not stores:
                    print(f"[locality] warn: {app} unresolved — exit-IP may be far from the "
                          f"spoofed GPS (see DEMAND_RADAR.md hardening notes)")
        except KeyboardInterrupt:
            print("\n[locality] interrupted — saving partial results")
        self._export(result)
        return result

    def _export(self, result):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        outdir = os.path.join(root, "exports")
        os.makedirs(outdir, exist_ok=True)
        slug = re.sub(r"\W+", "_", str(result["locality"]).lower()).strip("_")
        path = os.path.join(outdir, f"locality_{slug}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        print(f"[locality] mapping exported -> {path}")
        rows = self.db.darkstores()
        print(f"[locality] darkstores table now holds {len(rows)} row(s)")


def qc_health(cfg, apps=None):
    """
    One-probe-per-app QC readiness check (no DB writes). Probes each enabled
    quick-commerce app at the locality's first anchor and reports whether we
    get products, stock states, store identity and ETA. Run via
    `python3 run.py --qc-status`.
    """
    loc_cfg = cfg.get("demand", {}).get("locality", {}) or {}
    anchors = build_anchors(loc_cfg)
    if not anchors:
        anchors = [{"label": "fallback", "lat": 19.13129, "lon": 72.82463, "kind": "landmark"}]
    pt = next((a for a in anchors if a["kind"] == "landmark"), anchors[0])
    want = {a.strip().lower() for a in (apps or []) if a.strip()}
    corridor = Corridor(cfg.get("geo", {}).get("corridor", []) or [])
    rows = []
    print(f"[qc-status] anchor: {pt['label']} ({pt['lat']:.4f},{pt['lon']:.4f})")
    for app in QC_APPS:
        ad = cfg.get("adapters", {}).get(app, {})
        if not ad.get("enabled", True) or (want and app not in want):
            continue
        adapter = QC_APPS[app](cfg, corridor, [])
        t0 = time.time()
        try:
            products, meta = adapter.health_probe(pt["label"], pt["lat"], pt["lon"])
        except Exception as ex:
            products, meta = [], {"error": str(ex)[:120]}
        n = len(products)
        stamped = sum(1 for p in products if p.get("in_stock") is not None)
        pick = pick_store(meta.get("store_candidates"))
        err = meta.get("error")
        verdict = ("OK" if n and stamped else
                   "PARTIAL (ids only)" if pick and not n else
                   f"BLOCKED ({err[:40]})" if err else "EMPTY")
        rows.append({
            "app": app, "products": n, "stock_stamped": stamped,
            "store_id": pick["store_id"] if pick else None,
            "eta_min": meta.get("eta_min"), "sec": round(time.time() - t0, 1),
            "verdict": verdict,
        })
        print(f"  {app:<10} {verdict:<26} products={n:<4} stock={stamped:<4} "
              f"store={pick['store_id'] if pick else '—'} eta={meta.get('eta_min')} "
              f"({rows[-1]['sec']}s)")
    return {"anchor": pt, "apps": rows}


if __name__ == "__main__":
    # tiny offline sanity check of the pure helpers (no network)
    demo_cfg = {
        "demand": {"locality": {
            "name": "Andheri West",
            "bbox": {"min_lat": 19.103, "max_lat": 19.160,
                     "min_lon": 72.820, "max_lon": 72.855},
            "grid_step_m": 900,
            "landmarks": ["Versova", "Lokhandwala", "DN Nagar",
                          {"name": "Custom Point", "lat": 19.125, "lon": 72.835}],
        }},
    }
    pts = build_anchors(demo_cfg["demand"]["locality"])
    kinds = [p["kind"] for p in pts]
    print(f"{len(pts)} anchors · {kinds.count('landmark')} landmarks · {kinds.count('grid')} grid")
    for p in pts[:5]:
        print("  ", p)
    cand = [{"key": "store_id", "value": "40231", "count": 7},
            {"key": "warehouse_id", "value": "W9", "count": 2}]
    print("pick_store:", pick_store(cand))
