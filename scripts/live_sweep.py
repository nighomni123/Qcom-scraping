#!/usr/bin/env python3
"""
live_sweep.py — hunt for REAL glitched prices across the Mumbai corridor.

Runs the actual browser-intercept adapters at selected stations, records every
observation into deals.db (building baselines as it goes), evaluates each price
through the detector, and prints:

  1. Any flagged glitches (honey-pot / z-score / unit-error heuristics)
  2. A ranked table of the biggest discounts-vs-MRP seen (glitch candidates)

Usage:
  python3 scripts/live_sweep.py                          # all apps, core stations
  python3 scripts/live_sweep.py --apps blinkit zepto     # subset
  python3 scripts/live_sweep.py --stations Andheri Virar # subset
"""
from __future__ import annotations
import sys, os, argparse, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import run as cfgmod
from src.store import Store
from src.geo import Corridor, resolve_store
from src.honey import load_honey, honey_for_app
from src.detect import evaluate
from src.adapters.blinkit import BlinkitAdapter
from src.adapters.instamart import InstamartAdapter
from src.adapters.zepto import ZeptoAdapter


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apps", nargs="*", default=["blinkit", "zepto", "instamart"])
    ap.add_argument("--stations", nargs="*",
                    default=["Virar", "Bhayandar", "Malad", "Andheri"])
    ap.add_argument("--min-off-pct", type=float, default=55.0,
                    help="report items discounted >= this vs MRP")
    ap.add_argument("--from-db", action="store_true",
                    help="skip crawling; analyze price_obs already in deals.db")
    args = ap.parse_args()

    cfg = cfgmod.load_cfg()
    corridor = Corridor(cfg["geo"]["corridor"])
    stations = [s for s in corridor.stations if s["station"] in set(args.stations)] or \
               corridor.stations[:1]
    honey = load_honey(cfg)
    store = Store(cfg.get("db", "deals.db"))

    makers = {"blinkit": BlinkitAdapter, "instamart": InstamartAdapter, "zepto": ZeptoAdapter}
    adapters = []
    for name in args.apps:
        if name in makers:
            ad = makers[name](cfg, corridor, honey)
            ad.honey_app = name          # honey basket is keyed by real app name
            adapters.append(ad)
    if not adapters:
        print("no adapters selected"); return

    seen = []   # (app, station, product)
    t0 = time.time()
    if args.from_db:
        # Rebuild observations from what previous sweeps already recorded.
        rows = store.conn.execute(
            "SELECT app, store_id, sku_key, name, price, mrp, url FROM price_obs"
        ).fetchall()
        for appname, sid, sku, name, price, mrp, url in rows:
            station = sid.split("::")[-1].split("@")[0].replace("}", "").strip()
            seen.append((appname, station or sid,
                         {"sku_key": sku, "name": name, "price": price,
                          "mrp": mrp, "url": url or ""}))
        print(f"[from-db] loaded {len(seen)} observations")
    else:
        for s in stations:
            station, lat, lon = s["station"], s["lat"], s["lon"]
            store_id, store_label = resolve_store("multi", station, lat, lon)
            for ad in adapters:
                try:
                    prods = ad.crawl(station, lat, lon)
                except Exception as ex:
                    print(f"[warn] {ad.name}@{station}: {str(ex)[:100]}")
                    continue
                print(f"[sweep] {ad.name} @ {station}: {len(prods)} products")
                for p in prods:
                    if not p.get("price"):
                        continue
                    store.record(ad.name, store_id, p["sku_key"], p.get("name"),
                                 p["price"], p.get("mrp"), p.get("url", ""))
                    seen.append((ad.name, station, p))

    # ---- evaluate every observation through the real detector ----
    flags = []
    for appname, station, p in seen:
        g, score, reason = evaluate(
            appname, f"{appname}::{station}", p["sku_key"], p["price"],
            p.get("mrp"), cfg, store,
            honey_for_app(appname, honey), name=p.get("name"))
        if g:
            flags.append((appname, station, p, reason, score))

    # ---- classic unit-error heuristic: huge discount AND cheap absolute price ----
    unit_err = []
    for appname, station, p in seen:
        mrp, price = p.get("mrp"), p["price"]
        if mrp and mrp > 0:
            off = (mrp - price) / mrp * 100
            if off >= args.min_off_pct and price <= 99:
                unit_err.append((off, appname, station, p))
    unit_err.sort(reverse=True, key=lambda x: x[0])

    # ---- ranked discount table ----
    ranked = []
    for appname, station, p in seen:
        mrp, price = p.get("mrp"), p["price"]
        if mrp and mrp > 0 and price < mrp:
            ranked.append(((mrp - price) / mrp * 100, appname, station, p))
    ranked.sort(reverse=True, key=lambda x: x[0])

    print(f"\n=== SWEEP DONE in {time.time()-t0:.0f}s · {len(seen)} observations ===")

    print(f"\n--- DETECTOR FLAGS ({len(flags)}) ---")
    for appname, station, p, reason, score in flags[:15]:
        print(f"  🚨 {appname} @ {station}: ₹{p['price']:.0f} (mrp ₹{p.get('mrp') or '—'}) "
              f"— {p['name'][:50]} | {reason}")

    print(f"\n--- UNIT-ERROR CANDIDATES ≥{args.min_off_pct:.0f}% off & ≤₹99 ({len(unit_err)}) ---")
    for off, appname, station, p in unit_err[:12]:
        print(f"  💥 -{off:.0f}%  ₹{p['price']:.0f} (was ₹{p.get('mrp'):.0f})  {appname}@{station}"
              f"  {p['name'][:48]}  {p.get('url','')[:60]}")

    print("\n--- TOP DISCOUNTS SEEN (context) ---")
    for off, appname, station, p in ranked[:10]:
        print(f"  -{off:.0f}%  ₹{p['price']:.0f}/₹{p.get('mrp'):.0f}  {appname}@{station}  {p['name'][:48]}")

    if flags:
        print("\n=== GLITCH ALERTS ALSO FIRED THROUGH THE LIVE PIPELINE ABOVE ===")


if __name__ == "__main__":
    main()
