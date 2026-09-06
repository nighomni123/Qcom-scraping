"""
inventory.py — per-app darkstore INVENTORY capture (single-store, full-category).

This is the Product-Space Intelligence capture layer (M1). Given ONE store
(app + store_id), it runs a full every-category sweep and writes a RICH,
COMPLETE record to the per-app database:

    inventory_<app>.db  ->  inventory_catalog  (url + raw_json + collections +
                                                 price/mrp/in_stock/name)

while ALSO writing the operational catalog snapshot to deals.db
(catalog_snapshots / watchlist / churn) so Demand Radar, --embed-catalog and the
union layer keep working.

Unlike the old near-me mapper, this targets a specific store — not "where am I".
Location is resolved from (a) explicit --lat/--lon, or (b) the store's lat/lon in
deals.db darkstores. The app then serves exactly that store. No spoofing beyond
telling each app the truth about where the store is.

The crawler harvest already returns url + the full raw node; we persist both. The
capture is COMPLETE (including vouchers) — business filtering (voucher/assortment
exclusion) is a downstream concern, applied later by the union layer / Demand
Radar, never here.
"""
from __future__ import annotations

import copy
import math
import os
import time

from .store import Store
from .locality import LocalityMapper, QC_APPS

DEFAULT_RADIUS_M = 3500          # (retained for build_locality_cfg / future use)
DB_NAME_TEMPLATE = "inventory_{app}.db"
DB_DIR = "inventory"            # subfolder holding the per-app inventory databases


def build_locality_cfg(lat, lon, radius_m=DEFAULT_RADIUS_M,
                       name="current location"):
    """A locality config centered on (lat, lon): one landmark (the point
    itself, probed first) + a center-out bbox grid at the demand.grid_step_m
    spacing."""
    dlat = radius_m / 111_320.0
    dlon = radius_m / (111_320.0 * max(math.cos(math.radians(lat)), 0.01))
    return {
        "name": name,
        "bbox": {"min_lat": lat - dlat, "max_lat": lat + dlat,
                 "min_lon": lon - dlon, "max_lon": lon + dlon},
        "landmarks": [{"name": "current location", "lat": lat, "lon": lon}],
    }


def run_inventory(cfg, app, store_id, lat=None, lon=None,
                  mirror_page_ms=None, tabs=None):
    """
    Capture ONE store's full-category inventory.

    Targets `app`/`store_id`, runs a complete every-category sweep, and writes:
      * a rich record (url + raw_json + collections + price/mrp/in_stock/name)
        to inventory_<app>.db (inventory_catalog), and
      * the operational catalog snapshot to deals.db (catalog_snapshots /
        watchlist / churn) for continuity with Demand Radar / --embed-catalog.

    Location: --lat/--lon override wins; else looked up from deals.db
    darkstores; else SystemExit (pass --lat/--lon or map the store first).
    """
    app = (app or "").strip().lower()
    if app not in QC_APPS:
        raise SystemExit(f"[inventory] unknown app: {app!r} — known: {sorted(QC_APPS)}")
    if not store_id:
        raise SystemExit("[inventory] --store <store_id> is required")

    # Resolve the store's location.
    if lat is None or lon is None:
        deals = Store(cfg.get("db", "deals.db"))
        hit = deals.conn.execute(
            "SELECT lat, lon FROM darkstores WHERE app=? AND store_id=? LIMIT 1",
            (app, store_id)).fetchone()
        deals.close()
        if hit and hit[0] is not None:
            lat, lon = hit[0], hit[1]
            print(f"[inventory] store location from deals.db darkstores: "
                  f"{lat:.5f},{lon:.5f} ({app}:{store_id})")
        else:
            raise SystemExit(
                f"[inventory] no --lat/--lon given and {app}:{store_id} not in "
                f"deals.db darkstores — run `--map-locality` first or pass "
                f"--lat/--lon explicitly")

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root
    inv_dir = os.path.join(root, DB_DIR)
    os.makedirs(inv_dir, exist_ok=True)               # keep the subfolder present
    db_path = os.path.join(inv_dir, DB_NAME_TEMPLATE.format(app=app))
    inv_db = Store(db_path)

    print(f"[inventory] single-store capture: {app}:{store_id} @ ({lat:.5f},{lon:.5f})")
    print(f"[inventory]   rich capture -> {os.path.basename(db_path)} (inventory_catalog)")
    print(f"[inventory]   operational  -> deals.db (catalog_snapshots / watchlist / churn)")

    from .watchlist import WatchlistBuilder
    # deals.db Store is the operational target; inv_db is the rich capture target.
    wb = WatchlistBuilder(cfg, Store(cfg.get("db", "deals.db")), inventory_db=inv_db)
    wb.build_one_store(app, store_id, lat, lon,
                       catalog=True, skip_override=[], deep_cats=True,
                       mirror_page_ms=mirror_page_ms, tabs=tabs)

    n = inv_db.conn.execute(
        "SELECT COUNT(*) FROM inventory_catalog WHERE app=? AND store_id=?",
        (app, store_id)).fetchone()[0]
    with_url = inv_db.conn.execute(
        "SELECT COUNT(*) FROM inventory_catalog WHERE app=? AND store_id=? AND url<>''",
        (app, store_id)).fetchone()[0]
    with_raw = inv_db.conn.execute(
        "SELECT COUNT(*) FROM inventory_catalog WHERE app=? AND store_id=? "
        "AND raw_json IS NOT NULL",
        (app, store_id)).fetchone()[0]
    inv_db.close()
    print(f"[inventory] done: {app}:{store_id} -> {n} SKUs captured "
          f"({with_url} with url, {with_raw} with raw_json) in "
          f"{os.path.basename(db_path)}")
    return {"app": app, "store_id": store_id, "db": db_path,
            "skus": n, "with_url": with_url, "with_raw": with_raw}


if __name__ == "__main__":
    # Offline sanity check (no network): single-anchor cfg + per-app DB isolation
    # + a rich-capture round-trip through Store.upsert_inventory_catalog.
    import tempfile
    lc = build_locality_cfg(19.0728, 72.8826, radius_m=2000)
    from .locality import build_anchors
    pts = build_anchors(lc)
    kinds = [p["kind"] for p in pts]
    assert len(pts) >= 1, "expected at least one anchor"
    print(f"anchors: {len(pts)} ({kinds.count('landmark')} landmark + "
          f"{kinds.count('grid')} grid); first={pts[0]['label']}")

    tmp = tempfile.mkdtemp()
    s = Store(os.path.join(tmp, "inventory_blinkit.db"))
    s.upsert_inventory_catalog(
        "blinkit", "B1",
        {"sku_key": "k1", "name": "Amul Taaza Toned Milk 500ml", "price": 29.0,
         "mrp": 33.0, "in_stock": 1, "url": "https://x/y",
         "collections": ["home", "Milk"], "raw": {"id": "k1", "nested": {"a": 1}}})
    row = s.conn.execute(
        "SELECT name, url, category, raw_json, raw_json_bytes FROM inventory_catalog "
        "WHERE sku_key='k1'").fetchone()
    assert row and row[0] == "Amul Taaza Toned Milk 500ml" and row[1] == "https://x/y", row
    assert row[3] and "nested" in row[3], "raw_json should round-trip"
    s.close()
    print("per-app rich-capture round-trip OK")
