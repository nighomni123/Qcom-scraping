"""
density.py — M4 Phase 5: per-category local density + kNN distance with
minimum-N coverage guards.

Pure analysis over the M1 union layer (`load_product_space` rows). No new DB
tables, no scraping, no embedding math. This is the quantitative structure that
later phases (M5 internal/attribute gaps, M6 scoring) score against.

The plan's single most important guard lives here:

  * insufficient_coverage — a category with fewer than `min_n` tracked product
    groups across the 3 apps is flagged; NO gap candidate may be emitted from it.
    A "sparse" point there most likely means "we haven't watchlisted this
    subcategory yet", not a real market gap. This directly prevents the original
    plan's core failure mode (mistaking crawl gaps for market gaps) given this
    repo's ~1-store/day crawl cadence.
  * stale_coverage — a category whose last catalog sweep is older than
    `stale_days` is flagged so a gap isn't treated as current.

Distance is computed in a per-category NORMALIZED (unit_price, pack_value) space
so ₹/ml and grams compare fairly within a category (a 2-D projection of the
attribute vector's first two dims). Groups lacking both numerics get a None
neighborhood (they still appear in the table, but contribute no false neighbors).
"""
from __future__ import annotations

import math
import time
from collections import defaultdict


def _coord(unit_price, pack_value):
    """2-D position in (unit_price, pack_value); None if neither is known."""
    if unit_price is None and pack_value is None:
        return None
    return (unit_price or 0.0, pack_value or 0.0)


def _norm_coords(coords):
    """Scale a category's (up, pv) coords to [0,1] per axis for fair distance."""
    xs = [c[0] for c in coords if c and c[0] is not None]
    ys = [c[1] for c in coords if c and c[1] is not None]
    xmax = max(xs) if xs else 1.0
    ymax = max(ys) if ys else 1.0
    out = []
    for c in coords:
        if not c:
            out.append(None)
            continue
        x = (c[0] / xmax) if c[0] is not None else 0.0
        y = (c[1] / ymax) if c[1] is not None else 0.0
        out.append((x, y))
    return out


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def compute_density(rows, min_n=25, stale_days=21, db=None,
                    apps=("blinkit", "zepto", "instamart"), neighbor_radius=0.15):
    """Return a density table: one row per product_group_id.

    Each row: product_group_id, category, local_density, knn_distance,
    neighbor_count, neighbor_brands, coverage_n, coverage_age_days,
    insufficient_coverage, stale_coverage.
    """
    by_group = defaultdict(list)
    for r in rows:
        gid = r.get("product_group_id")
        if gid is None:
            continue
        by_group[gid].append(r)

    # per-group meta + per-category grouping
    meta = {}
    cat_groups = defaultdict(list)
    for gid, members in by_group.items():
        cat = max((m.get("category") or "" for m in members),
                  key=lambda c: sum(1 for m in members if (m.get("category") or "") == c))
        ups = [m["unit_price"] for m in members if m.get("unit_price") is not None]
        pvs = [m["pack_value"] for m in members if m.get("pack_value") is not None]
        up = sum(ups) / len(ups) if ups else None
        pv = sum(pvs) / len(pvs) if pvs else None
        brands = {m.get("brand") for m in members if m.get("brand")}
        meta[gid] = {"category": cat, "coord": _coord(up, pv),
                     "brands": brands, "member_count": len(members)}
        cat_groups[cat].append(gid)

    # per-category normalized coords (index-aligned list)
    cat_norm = {}
    for cat, gids in cat_groups.items():
        cat_norm[cat] = _norm_coords([meta[g]["coord"] for g in gids])

    # coarse coverage age per category from the latest catalog snapshot overall
    cat_age = {}
    if db is not None:
        try:
            row = db.conn.execute("SELECT MAX(ts) FROM catalog_snapshots").fetchone()
            last = row[0] if row else None
            if last:
                age = (time.time() - last) / 86400.0
                for cat in cat_groups:
                    cat_age[cat] = age
        except Exception:
            pass

    results = []
    for cat, gids in cat_groups.items():
        coverage_n = len(gids)
        insufficient = coverage_n < min_n
        stale = bool(cat_age.get(cat) is not None and cat_age[cat] > stale_days)
        norm = cat_norm[cat]
        for i, gid in enumerate(gids):
            c = meta[gid]["coord"]
            nc = norm[i]
            base = {
                "product_group_id": gid, "category": cat,
                "coverage_n": coverage_n,
                "coverage_age_days": round(cat_age[cat], 1) if cat_age.get(cat) else None,
                "insufficient_coverage": insufficient, "stale_coverage": stale,
            }
            if nc is None or insufficient:
                # no numeric neighborhood, or whole category too thin to trust
                results.append({**base, "local_density": None, "knn_distance": None,
                                "neighbor_count": None, "neighbor_brands": None})
                continue
            best = None
            nb_brands = set()
            nb_count = 0
            for j, og in enumerate(gids):
                if j == i:
                    continue
                oc = norm[j]
                if oc is None:
                    continue
                d = _dist(nc, oc)
                if best is None or d < best:
                    best = d
                if d <= neighbor_radius:
                    nb_count += 1
                    nb_brands |= meta[og]["brands"]
            results.append({**base,
                            "local_density": round(1.0 / (1.0 + (best or 0.0)), 4),
                            "knn_distance": round(best, 4) if best is not None else None,
                            "neighbor_count": nb_count,
                            "neighbor_brands": ",".join(sorted(b for b in nb_brands if b))})
    return results


def sparse_candidates(density_rows, knn_threshold=None):
    """M5 helper (kept here next to the structure it reads): return density rows
    that are locally sparse BUT trustworthy — i.e. NOT insufficient_coverage and
    NOT stale_coverage. `knn_threshold` (normalized distance) is optional; when
    None, every trustworthy sparse point (no neighbor within radius => knn large)
    is returned. This function NEVER returns a candidate from a guarded category.
    """
    out = []
    for r in density_rows:
        if r["insufficient_coverage"] or r["stale_coverage"]:
            continue
        if r["knn_distance"] is None:
            continue
        if knn_threshold is not None and r["knn_distance"] < knn_threshold:
            continue
        out.append(r)
    return out


if __name__ == "__main__":
    # Offline self-test (no DB): coverage guard + sparse detection.
    def mk(gid, cat, name, app, up, pv, brand):
        return dict(product_group_id=gid, category=cat, name=name, app=app,
                    store_id=app + "1", sku_key=app + "-" + name,
                    unit_price=up, pack_value=pv, brand=brand, in_stock=1)

    # "Spices" is a WELL-covered category (>= min_n) with one isolated group.
    rows = []
    for i in range(30):
        rows.append(mk(f"S{i}", "Spices", f"Spice {i}", "blinkit",
                       10.0 + i * 0.1, 100.0 + i, "Generic"))
    rows.append(mk("ISO", "Spices", "Rare Exotic Spice", "blinkit",
                   900.0, 50.0, "Exotic"))   # far from the cluster
    # "Rare" is UNDER-covered (< min_n) -> must be flagged insufficient
    rows.append(mk("R1", "Rare", "Odd Item 1", "blinkit", 20.0, 200.0, "X"))
    rows.append(mk("R2", "Rare", "Odd Item 2", "zepto", 25.0, 220.0, "X"))

    dens = compute_density(rows, min_n=25)
    by = {d["product_group_id"]: d for d in dens}

    # well-covered category: no insufficient flag
    assert not by["S0"]["insufficient_coverage"], "Spices should be well-covered"
    # isolated group has a large kNN distance and no close neighbors
    iso = by["ISO"]
    assert iso["knn_distance"] is not None and iso["knn_distance"] > 0.5, iso
    assert iso["neighbor_count"] == 0, iso
    # under-covered category: every group flagged insufficient
    assert by["R1"]["insufficient_coverage"] and by["R2"]["insufficient_coverage"]
    # sparse_candidates must NOT include guarded (insufficient) categories
    cands = sparse_candidates(dens)
    cand_ids = {c["product_group_id"] for c in cands}
    assert "R1" not in cand_ids and "R2" not in cand_ids, "guarded cats leaked"
    assert "ISO" in cand_ids, "trustworthy sparse group dropped"

    print("[density] self-test OK")
