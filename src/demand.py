"""
demand.py — Demand Radar phase 4: analysis rollups over stock_obs + oos_events.

All numbers are PROXIES for demand built from stock-out behaviour:

  DPI (Demand Pressure Index) per SKU
      = Σ over 'oos' events of duration_min × exp(-age_days / HALF_LIFE_DAYS)
        / observation_days

  where observation_days = days since that store's first stock_obs snapshot
  (floor 0.5 so a fresh store doesn't divide by ~0). Higher DPI = more
  frequent / recent / longer stock-outs. 'vanished' events are delistings,
  NOT demand — excluded here, surfaced separately.

  Resolution honesty (09-04): DPI measures demand pressure on PROBED
  shelves, not the whole inventory — the light sweep (home +
  categories_per_store shelves + watchlist search terms) sights a few
  hundred SKUs/cycle vs the watchlist's full catalog census (~8-24k
  rows/store). A never-sighted SKU's absence is a coverage gap, never a
  stock signal; whole-inventory delisting truth comes from catalog_events
  snapshot diffs.

  Restock velocity = mean minutes of CLOSED oos events (a SKU that comes back
  fast is being bought out fast).

Hours are LOCAL time (the operator's wall clock) — consistent across views.
Pure functions over sqlite; no network. Used by the dashboard (/demand,
/heatmap, /eta, /qc), `run.py --demand-report`, and CSV exports.

Vouchers / gift cards are NOT commodities (their stock-outs are code-pool
replenishments, not demand) — they are excluded at the source (watchlist
builder + prober) and again here defensively, so pre-purge or stray rows can
never re-enter the DPI table.
"""
from __future__ import annotations

import csv
import math
import os
import time

from .store import is_voucher_name

HALF_LIFE_DAYS = 3.0


def _now():
    return time.time()


def _recency_weight(ts, now):
    age_days = max(0.0, (now - ts)) / 86400.0
    return math.exp(-age_days * math.log(2) / HALF_LIFE_DAYS)


def _name_map(db, store_id=None):
    """sku_key -> best known product name. Watchlist first; price_obs
    (glitch-monitor crawls + inventory captures, newest wins) as fallback —
    the prober records stock for SKUs it merely passes by in search results,
    which never land in the watchlist and would otherwise show anonymous
    in the DPI table / AI digests."""
    q = "SELECT sku_key, name FROM watchlist" + (" WHERE store_id=?" if store_id else "")
    rows = db.conn.execute(q, (store_id,) if store_id else ()).fetchall()
    names = {r[0]: r[1] for r in rows if r[1]}
    fallback = {}
    for sku, name in db.conn.execute(
            "SELECT sku_key, name FROM price_obs WHERE name IS NOT NULL ORDER BY ts"):
        fallback[sku] = name              # ascending ts -> newest wins
    for sku, name in fallback.items():
        names.setdefault(sku, name)
    return names


def observation_span_days(db, store_id=None):
    """Days of collected observations (floor 0.5)."""
    q = "SELECT MIN(ts) FROM stock_obs" + (" WHERE store_id=?" if store_id else "")
    row = db.conn.execute(q, (store_id,) if store_id else ()).fetchone()
    if not row or not row[0]:
        return 0.5
    return max(0.5, (_now() - row[0]) / 86400.0)


def dpi_table(db, store_id=None, since_days=7, limit=50):
    """
    Ranked [{sku_key,name,dpi,n_events,total_min,mean_restock_min,last_price,
             last_seen,active}] — highest pressure first.
    """
    now = _now()
    since = now - since_days * 86400
    names = _name_map(db, store_id)
    obs_days = observation_span_days(db, store_id)

    ev_q = ("SELECT sku_key, started_at, ended_at, snapshots FROM oos_events "
            "WHERE kind='oos' AND started_at>=?" + (" AND store_id=?" if store_id else ""))
    args = [since] + ([store_id] if store_id else [])
    agg = {}
    for sku, started, ended, snaps in db.conn.execute(ev_q, args):
        a = agg.setdefault(sku, {"n": 0, "total_min": 0.0, "weighted": 0.0,
                                 "closed_min": []})
        dur_min = ((ended or now) - started) / 60.0
        a["n"] += 1
        a["total_min"] += dur_min
        a["weighted"] += dur_min * _recency_weight(started, now)
        if ended:
            a["closed_min"].append(dur_min)

    last_q = ("SELECT sku_key, price, MAX(ts) FROM stock_obs "
              + ("WHERE store_id=?" if store_id else "") +
              " GROUP BY sku_key")
    largs = [store_id] if store_id else []
    last_price, last_seen = {}, {}
    active = {r[0] for r in db.conn.execute(
        "SELECT sku_key FROM watchlist WHERE active=1"
        + (" AND store_id=?" if store_id else ""), largs)}
    for sku, price, ts in db.conn.execute(last_q, largs):
        last_price[sku] = price
        last_seen[sku] = ts

    rows = []
    for sku, a in agg.items():
        name = names.get(sku)
        if name and is_voucher_name(name):
            continue          # vouchers are not commodities — never in the radar
        closed = sorted(a["closed_min"])
        mean_restock = round(sum(closed) / len(closed), 1) if closed else None
        rows.append({
            "sku_key": sku,
            "name": name or sku,
            "dpi": round(a["weighted"] / obs_days, 2),
            "n_events": a["n"],
            "total_min": round(a["total_min"], 1),
            "mean_restock_min": mean_restock,
            "last_price": last_price.get(sku),
            "last_seen": last_seen.get(sku),
            "active": sku in active,
        })
    rows.sort(key=lambda r: (-r["dpi"], -r["n_events"]))
    return rows[:limit]


def heatmap(db, store_id=None, top=20):
    """
    Hour-of-day (local) × top-DPI SKU matrix of OOS ONSET counts.
    Returns {hours:[0..23], skus:[{name,total,by_hour:[24]}], peak_hour}.
    """
    names = _name_map(db, store_id)
    ranked = dpi_table(db, store_id=store_id, limit=top)
    want = {r["sku_key"]: {"name": r["name"], "total": 0, "by_hour": [0] * 24}
            for r in ranked}
    ev_q = ("SELECT sku_key, started_at FROM oos_events WHERE kind='oos'"
            + (" AND store_id=?" if store_id else ""))
    args = [store_id] if store_id else []
    for sku, started in db.conn.execute(ev_q, args):
        cell = want.get(sku)
        if cell:
            hr = time.localtime(started).tm_hour
            cell["by_hour"][hr] += 1
            cell["total"] += 1
    skus = [v | {"sku_key": k} for k, v in want.items()]
    skus.sort(key=lambda s: (-s["total"], s["name"]))
    hourly_tot = [sum(s["by_hour"][h] for s in skus) for h in range(24)]
    peak = hourly_tot.index(max(hourly_tot)) if any(hourly_tot) else None
    return {"hours": list(range(24)), "skus": skus, "peak_hour": peak}


def eta_curve(db, store_id=None):
    """Per-local-hour delivery ETA stats from stock_obs.eta_min."""
    q = ("SELECT ts, eta_min FROM stock_obs WHERE eta_min IS NOT NULL"
         + (" AND store_id=?" if store_id else ""))
    args = [store_id] if store_id else []
    buckets = {h: [] for h in range(24)}
    for ts, eta in db.conn.execute(q, args):
        buckets[time.localtime(ts).tm_hour].append(eta)
    out = []
    for h in range(24):
        vals = buckets[h]
        out.append({"hour": h, "n": len(vals),
                    "avg": round(sum(vals) / len(vals), 1) if vals else None,
                    "min": min(vals) if vals else None,
                    "max": max(vals) if vals else None})
    return out


def demand_summary(db):
    """Per-darkstore overview + totals — feeds the dashboard's radar panel."""
    stores = []
    for app, sid, label, lat, lon, eta in db.darkstores():
        one = lambda q, a: db.conn.execute(q, a).fetchone()[0]
        stores.append({
            "app": app, "store_id": sid, "label": label,
            "obs": one("SELECT COUNT(*) FROM stock_obs WHERE store_id=?", (sid,)),
            "skus_seen": one("SELECT COUNT(DISTINCT sku_key) FROM stock_obs WHERE store_id=?", (sid,)),
            "active_skus": one("SELECT COUNT(*) FROM watchlist WHERE store_id=? AND active=1", (sid,)),
            "open_oos": one("SELECT COUNT(*) FROM oos_events WHERE store_id=? AND ended_at IS NULL AND kind='oos'", (sid,)),
            "vanished": one("SELECT COUNT(*) FROM oos_events WHERE store_id=? AND ended_at IS NULL AND kind='vanished'", (sid,)),
            "eta_min": eta,
        })
    totals = {
        "obs": sum(s["obs"] for s in stores),
        "open_oos": sum(s["open_oos"] for s in stores),
        "vanished": sum(s["vanished"] for s in stores),
        "obs_days": round(observation_span_days(db), 1),
    }
    return {"stores": stores, "totals": totals}


def export_csv(db, path=None):
    """Nightly-style DPI export → exports/dpi_<date>.csv. Returns path."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    outdir = os.path.join(root, "exports")
    os.makedirs(outdir, exist_ok=True)
    path = path or os.path.join(outdir, f"dpi_{time.strftime('%Y%m%d')}.csv")
    rows = dpi_table(db, limit=10000)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["rank", "app", "store_id", "sku_key", "name", "dpi",
                    "oos_events", "total_oos_min", "mean_restock_min",
                    "last_price", "active"])
        store_meta = {s["store_id"]: s["app"] for s in demand_summary(db)["stores"]}
        for i, r in enumerate(rows, 1):
            sku = r["sku_key"]
            # watchlist PK is (app, store_id, sku_key) — resolve app+store per row
            m = db.conn.execute(
                "SELECT app, store_id FROM watchlist WHERE sku_key=? ORDER BY last_seen_ts DESC LIMIT 1",
                (sku,)).fetchone()
            app, sid = (m[0], m[1]) if m else ("", "")
            w.writerow([i, app or store_meta.get(sid, ""), sid, sku, r["name"],
                        r["dpi"], r["n_events"], r["total_min"],
                        r["mean_restock_min"], r["last_price"], int(r["active"])])
    return path
