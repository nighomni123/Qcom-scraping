"""
inventory.py — per-app darkstore INVENTORY around the machine's real location.

Unlike --map-locality (a fixed configured locality feeding the shared
deals.db), this answers "which Blinkit / Instamart / Zepto darkstores serve
where I actually am right now?" and writes EACH APP into its OWN sqlite file:

    inventory_blinkit.db · inventory_instamart.db · inventory_zepto.db

Location policy (deliberate): the anchor center is the machine's APPROXIMATE
PUBLIC-IP LOCATION (ipinfo.io, fallback ip-api.com) — we tell each app the
truth about where we are, no GPS spoofing to some other neighborhood. The
browser-intercept layer still enforces that same coordinate on every request
only so cached client-side locations can't silently serve a different city.
Override the auto-detected point with --lat/--lon if needed.

Discovery itself is the proven --map-locality machinery (LocalityMapper over a
small centered bbox grid, saturation early-stop, politeness gaps) pointed at a
per-app Store instance, so nothing here touches deals.db. Keep volumes modest:
this is research tooling, not bulk harvesting.
"""
from __future__ import annotations

import copy
import json
import math
import os
import time
import urllib.request

from .store import Store
from .locality import LocalityMapper, QC_APPS

DEFAULT_RADIUS_M = 3500          # bbox half-width around the resolved point
DEFAULT_MAX_POINTS = 12          # per-app probe cap (saturation stops earlier)
DB_NAME_TEMPLATE = "inventory_{app}.db"


def approx_location(timeout=8):
    """
    Approximate lat/lon + city from the machine's public IP. Honest lookup —
    no spoofing, just reading back where the network already places us.
    Returns {source, ip, city, region, country, lat, lon}.
    """
    def _ipinfo():
        with urllib.request.urlopen("https://ipinfo.io/json", timeout=timeout) as r:
            d = json.loads(r.read().decode())
        lat, lon = str(d["loc"]).split(",")
        return {"source": "ipinfo.io", "ip": d.get("ip"), "city": d.get("city"),
                "region": d.get("region"), "country": d.get("country"),
                "lat": float(lat), "lon": float(lon)}

    def _ipapi():
        url = ("http://ip-api.com/json/?fields=status,message,country,"
               "regionName,city,lat,lon,query")
        with urllib.request.urlopen(url, timeout=timeout) as r:
            d = json.loads(r.read().decode())
        if d.get("status") != "success":
            raise RuntimeError(str(d.get("message")))
        return {"source": "ip-api.com", "ip": d.get("query"), "city": d.get("city"),
                "region": d.get("regionName"), "country": d.get("country"),
                "lat": d["lat"], "lon": d["lon"]}

    last_err = None
    for fn in (_ipinfo, _ipapi):
        try:
            return fn()
        except Exception as ex:
            last_err = ex
    raise RuntimeError(f"could not resolve approximate location: {last_err} "
                       f"(pass --lat/--lon explicitly)")


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


def run_inventory(cfg, apps=None, lat=None, lon=None, radius_m=None,
                  max_points=None):
    """
    Map darkstores per app into separate inventory_<app>.db files.
    apps: subset of blinkit/instamart/zepto (default: all three, sequential).
    Returns {app: {"db": path, "stores": n}}.
    """
    radius_m = int(radius_m or DEFAULT_RADIUS_M)
    max_points = int(max_points or DEFAULT_MAX_POINTS)

    if lat is None or lon is None:
        loc = approx_location()
        lat, lon = loc["lat"], loc["lon"]
        where = f"{loc.get('city') or '?'}, {loc.get('region') or ''}".strip(", ")
        print(f"[inventory] approximate current location: {where} — "
              f"{lat:.5f},{lon:.5f} (via {loc['source']}, IP {loc.get('ip')}; "
              f"no spoofing)")
    else:
        print(f"[inventory] using explicit location: {lat:.5f},{lon:.5f}")

    want = [a.strip().lower() for a in (apps or list(QC_APPS)) if a.strip()]
    unknown = [a for a in want if a not in QC_APPS]
    if unknown:
        raise SystemExit(f"[inventory] unknown app(s): {unknown} — "
                         f"known: {sorted(QC_APPS)}")

    root = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(root)                      # repo root (like deals.db)
    summary = {}
    for i, app in enumerate(want):
        db_path = os.path.join(root, DB_NAME_TEMPLATE.format(app=app))
        app_cfg = copy.deepcopy(cfg)
        app_cfg["demand"] = dict(app_cfg.get("demand") or {})
        app_cfg["demand"]["locality"] = build_locality_cfg(
            lat, lon, radius_m, name=f"current {app}")
        print(f"\n[inventory] === {app} === ({i + 1}/{len(want)}) -> {db_path}")
        db = Store(db_path)
        try:
            mapper = LocalityMapper(app_cfg, db)
            result = mapper.map_locality(apps=[app], max_points=max_points)
            stores = result["apps"].get(app, {}).get("stores", [])
        finally:
            rows = db.darkstores(app)
            db.close()
        summary[app] = {"db": db_path, "stores": len(rows)}
        if rows:
            print(f"[inventory] {app}: {len(rows)} store(s) in {os.path.basename(db_path)}:")
            for sid, label, slat, slon in [(r[1], r[2], r[3], r[4]) for r in rows]:
                print(f"    {sid:<16} {str(label)[:44]:<46} ({slat:.5f},{slon:.5f})")
        else:
            print(f"[inventory] {app}: NO stores resolved — see warnings above "
                  f"(Instamart is known-gated pre-onboarding; exit-IP may also "
                  f"sit outside the app's service area)")
    print("\n[inventory] done: " +
          ", ".join(f"{a}={s['stores']}({os.path.basename(s['db'])})"
                    for a, s in summary.items()))
    return summary


if __name__ == "__main__":
    # offline sanity check (no network): grid shape + per-app DB isolation
    lc = build_locality_cfg(19.0728, 72.8826, radius_m=2000)
    from .locality import build_anchors
    pts = build_anchors(lc)
    kinds = [p["kind"] for p in pts]
    print(f"anchors: {len(pts)} ({kinds.count('landmark')} landmark + "
          f"{kinds.count('grid')} grid); first={pts[0]['label']}")
    import tempfile
    tmp = tempfile.mkdtemp()
    dbs = {}
    for app, sid in (("blinkit", "B1"), ("zepto", "Z9")):
        s = Store(os.path.join(tmp, f"inventory_{app}.db"))
        s.upsert_darkstore(app, sid, f"{app} demo", 19.07, 72.88, 12)
        dbs[app] = s
    assert dbs["blinkit"].darkstores("blinkit") and not dbs["blinkit"].darkstores("zepto")
    assert dbs["zepto"].darkstores("zepto") and not dbs["zepto"].darkstores("blinkit")
    for s in dbs.values():
        s.close()
    print("per-app database isolation OK")
