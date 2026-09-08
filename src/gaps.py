"""
gaps.py — M5 Phase 5 (internal / attribute gaps), the second gap type after
assortment gaps (M3).

Builds on the M4 density layer: takes the *trustworthy* sparse candidates
(excluded from any `insufficient_coverage` / `stale_coverage` category) and
scores them as INTERNAL / ATTRIBUTE gaps using attribute-vector proximity.

An internal/attribute gap = a product that is locally sparse in price/pack space
AND whose attribute configuration (unit_price, pack, multipack) sits far from the
category's typical configuration — i.e. a *missing variant* the neighborhood
implies should exist (e.g. a 1L sits alone when 500ml and 2L both sell well).

This is a transparent, guardrailed candidate generator. The final opportunity
score (assortment × DPI × coverage × churn) is M6's job — M5 only surfaces and
pre-scores the internal-gap candidates with reason codes.

Pure stdlib; reuses src.product_vectors.build_product_vectors (attribute_vector)
and src.density.compute_density / sparse_candidates. No new DB tables, no
scraping. Degrades offline (no `db` -> DPI fields are None, which M5 doesn't
require).
"""
from __future__ import annotations

import math
from collections import defaultdict

# Honesty thresholds: a point is only an internal/attribute gap when it is
# genuinely sparse (no neighbor within density.py's neighborhood radius) OR a
# true attribute outlier (config far from the category centroid in z-space).
# knn_distance is in density.py's normalized [0,1] unit_price/pack space.
SPARSE_KNN = 0.15
ATTR_OUTLIER_DIST = 0.5


def _attr_dist(a, b, dims=(0, 1, 2)):
    """Euclidean distance over the numeric attribute dims (unit_price_z,
    pack_z, is_multipack)."""
    s = 0.0
    for d in dims:
        s += (a[d] - b[d]) ** 2
    return math.sqrt(s)


def score_internal_gaps(rows, min_n=25, db=None, knn_weight=0.5, attr_weight=0.5):
    """Return internal/attribute gap candidates (highest score first).

    Each candidate dict:
        product_group_id, category, knn_distance, attr_distance,
        neighbor_count, neighbor_brands, internal_gap_score, reason_codes
    """
    from .product_vectors import build_product_vectors
    from .density import compute_density, sparse_candidates

    vecs = build_product_vectors(rows, db=db)          # gid -> {attribute_vector, ...}
    dens = compute_density(rows, min_n=min_n, db=db)   # per-gid density rows

    by_gid = {d["product_group_id"]: d for d in dens}
    # per-category list of group ids (from vectors meta)
    by_cat = defaultdict(list)
    for gid, v in vecs.items():
        by_cat[v["category"]].append(gid)

    # precompute per-category attribute centroid (dims 0,1,2) over all members
    cat_centroid = {}
    for cat, gids in by_cat.items():
        pts = [vecs[g]["attribute_vector"] for g in gids if g in vecs]
        if not pts:
            continue
        ndim = len(pts[0])
        cen = [sum(p[d] for p in pts) / len(pts) for d in range(ndim)]
        cat_centroid[cat] = cen

    out = []
    for cand in sparse_candidates(dens):
        # sparse_candidates already excludes insufficient/stale categories
        gid = cand["product_group_id"]
        if gid not in vecs:
            continue
        cat = vecs[gid]["category"]
        neighbors = [h for h in by_cat.get(cat, []) if h != gid]
        if len(neighbors) < 2:
            # can't be an internal gap without a populated neighborhood
            continue
        attr = vecs[gid]["attribute_vector"]
        centroid = cat_centroid.get(cat)
        if centroid is None:
            continue
        attr_dist = _attr_dist(attr, centroid)
        knn = cand["knn_distance"] or 0.0

        # Honesty guard: only genuinely sparse / outlier points are gaps.
        # knn > SPARSE_KNN means no neighbor within the radius density.py uses
        # to define neighborhoods -> a real empty pocket, not a busy cluster.
        sparse = knn > SPARSE_KNN
        outlier = attr_dist >= ATTR_OUTLIER_DIST
        if not (sparse or outlier):
            continue

        # transparent score: z-scored sparsity (knn in [0,1]) blended with
        # z-space attribute outlier-ness (clamped to [0,1] for the blend).
        knn_part = min(knn, 1.0)
        attr_part = min(attr_dist, 1.0)
        score = round(knn_weight * knn_part + attr_weight * attr_part, 4)

        reason = []
        if sparse:
            reason.append("sparse_in_price_pack_space")
        if outlier:
            reason.append("attribute_outlier_vs_category")
        reason.append("neighbors_present_in_category")

        out.append({
            "product_group_id": gid,
            "category": cat,
            "knn_distance": round(knn, 4),
            "attr_distance": round(attr_dist, 4),
            "neighbor_count": cand["neighbor_count"],
            "neighbor_brands": cand["neighbor_brands"],
            "internal_gap_score": score,
            "reason_codes": reason,
        })

    out.sort(key=lambda c: -c["internal_gap_score"])
    return out


if __name__ == "__main__":
    # Offline self-test (no DB): an attribute outlier in a well-covered category
    # must surface as an internal gap; under-covered categories must not.
    def mk(gid, cat, name, up, pv, brand, app="blinkit"):
        return dict(product_group_id=gid, category=cat, name=name, app=app,
                    store_id=app + "1", sku_key=app + "-" + name,
                    unit_price=up, pack_value=pv, brand=brand, in_stock=1)

    rows = []
    for i in range(30):
        rows.append(mk(f"S{i}", "Spices", f"Spice {i}", 10.0 + i * 0.1,
                       100.0 + i, "Generic"))
    # attribute outlier: extreme unit_price + tiny pack vs the cluster
    rows.append(mk("ISO", "Spices", "Rare Exotic Spice", 900.0, 50.0, "Exotic"))
    # under-covered category -> must be excluded by the guard
    rows.append(mk("R1", "Rare", "Odd Item 1", 20.0, 200.0, "X"))
    rows.append(mk("R2", "Rare", "Odd Item 2", 25.0, 220.0, "X"))

    gaps = score_internal_gaps(rows, min_n=25)
    ids = {g["product_group_id"] for g in gaps}
    assert "ISO" in ids, "attribute outlier must be flagged as internal gap"
    assert "R1" not in ids and "R2" not in ids, "guarded category leaked"
    iso = next(g for g in gaps if g["product_group_id"] == "ISO")
    assert "attribute_outlier_vs_category" in iso["reason_codes"]
    assert "sparse_in_price_pack_space" in iso["reason_codes"]
    assert iso["internal_gap_score"] > 0.5

    print("[gaps] self-test OK")
