"""
product_vectors.py — M2 Phase 3B/3C: grocery-shaped attribute + commercial
vectors per cross-app product group.

Reuses the M1 union layer (src.product_space.load_product_space) and the Demand
Radar rollup (src.demand.dpi_table) — it adds NO new DB tables, does NO
scraping, and only READS existing data. Output: a dict keyed by
`product_group_id` carrying:

  attribute_vector  [4 floats]  z-scored PER CATEGORY (so ₹/ml compares only
                                within a category):
                                  [0] unit_price (imputed w/ flag when missing)
                                  [1] pack_size_value (canonical ml/g/count)
                                  [2] is_multipack (0/1)
                                  [3] category_depth (int)
  commercial_features {dict}     dpi (from Demand Radar rollup), store_count,
                                app_count, days_since_first_seen,
                                is_currently_active, recent_churn_flag
  category, rep_name, member_count, present_apps, flags, unit_price, pack_value

Honesty invariants (from PRODUCT_SPACE_PLAN.md):
  * a missing unit_price becomes None + an imputation flag, NEVER 0.
  * confidence/dpi measure coverage/pressure, never sales volume.
  * vouchers are excluded defensively (re-check is_voucher_name), though the
    union layer already strips them.
  * every DB-derived field degrades to None/0 when no `db` is supplied, so the
    self-test runs fully offline.

ponytail: pure stdlib (math/statistics) — no numpy dependency, so the module
and its self-test run anywhere Python 3.8+ runs.
"""
from __future__ import annotations

import statistics
import sys
import time
from collections import Counter, defaultdict

from .store import is_voucher_name


def _mean_std(xs):
    """(mean, population_std) or (None, 0.0) for empty input."""
    if not xs:
        return (None, 0.0)
    m = statistics.fmean(xs)
    sd = statistics.pstdev(xs) if len(xs) > 1 else 0.0
    return (m, sd)


def _z(value, mean, std):
    """Z-score; 0.0 when there's no spread or nothing to compare against."""
    if mean is None or value is None:
        return 0.0
    if std and std > 0:
        return (value - mean) / std
    return 0.0


def _cat_depth(cat):
    cat = cat or ""
    return len(cat.split(">")) if ">" in cat else 1


def build_product_vectors(rows, db=None):
    """Build per-group attribute + commercial vectors.

    `rows` = list[dict] from load_product_space(). `db` = an optional
    src.store.Store instance (enables dpi / first-seen / churn enrichment).
    Returns dict[product_group_id -> {...}].
    """
    # 1) group rows by product_group_id, dropping vouchers defensively
    groups = defaultdict(list)
    for r in rows:
        gid = r.get("product_group_id")
        if gid is None:
            continue
        if is_voucher_name(r.get("name", "")):
            continue
        groups[gid].append(r)

    # 2) PER-CATEGORY stats for honest z-scoring of unit_price / pack_value
    cat_up = defaultdict(list)
    cat_pv = defaultdict(list)
    for members in groups.values():
        for m in members:
            c = m.get("category") or ""
            if m.get("unit_price") is not None:
                cat_up[c].append(m["unit_price"])
            if m.get("pack_value") is not None:
                cat_pv[c].append(m["pack_value"])
    cat_up_stats = {c: _mean_std(v) for c, v in cat_up.items()}
    cat_pv_stats = {c: _mean_std(v) for c, v in cat_pv.items()}

    # 3) optional DB enrichment (Demand Radar rollup + churn + first-seen)
    dpi_map = {}
    wl_active_skus = set()
    first_seen_map = {}
    churn_skus = set()
    if db is not None:
        try:
            from .demand import dpi_table
            for d in dpi_table(db, since_days=30, limit=10 ** 9):
                if d.get("dpi") is not None:
                    dpi_map[d["sku_key"]] = d["dpi"]
        except Exception:
            pass
        try:
            for sku, ts, active in db.conn.execute(
                "SELECT sku_key, first_seen_ts, active FROM watchlist"
            ):
                if ts:
                    first_seen_map[sku] = ts
                if active:
                    wl_active_skus.add(sku)
        except Exception:
            pass
        try:
            cutoff = time.time() - 14 * 86400.0
            for (sku,) in db.conn.execute(
                "SELECT sku_key FROM catalog_events "
                "WHERE kind IN ('new','delisted') AND ts>=?",
                (cutoff,),
            ):
                churn_skus.add(sku)
        except Exception:
            pass

    now = time.time()
    out = {}
    for gid, members in groups.items():
        ups = [m["unit_price"] for m in members if m.get("unit_price") is not None]
        pvs = [m["pack_value"] for m in members if m.get("pack_value") is not None]
        cat = Counter(m.get("category") or "" for m in members).most_common(1)[0][0]

        up_mean = statistics.fmean(ups) if ups else None
        pv_mean = statistics.fmean(pvs) if pvs else None
        up_stats = cat_up_stats.get(cat)
        pv_stats = cat_pv_stats.get(cat)

        flags = {}
        # unit_price: z-score within category; impute category median if absent
        if up_mean is None:
            if up_stats and up_stats[0] is not None:
                up_z = _z(up_stats[0], *up_stats)
                up_val = up_stats[0]
                flags["unit_price_imputed"] = True
            else:
                up_z, up_val = 0.0, None
                flags["unit_price_missing"] = True
        else:
            up_z = _z(up_mean, *up_stats) if up_stats else 0.0
            up_val = up_mean
        # pack_size: z-score within category; 0 + flag if absent
        if pv_mean is None:
            pv_z, pv_val = 0.0, None
            flags["pack_value_missing"] = True
        else:
            pv_z = _z(pv_mean, *pv_stats) if pv_stats else 0.0
            pv_val = pv_mean

        is_mp = 1 if any(m.get("is_multipack") for m in members) else 0
        attribute_vector = [
            round(up_z, 4), round(pv_z, 4), float(is_mp), float(_cat_depth(cat))
        ]

        skus = [m.get("sku_key") for m in members]
        g_dpi = [dpi_map[s] for s in skus if s in dpi_map]
        dpi = round(statistics.fmean(g_dpi), 3) if g_dpi else None
        store_count = len({m.get("store_id") for m in members})
        app_count = len({m.get("app") for m in members})
        fss = [first_seen_map[s] for s in skus if s in first_seen_map]
        days_since_first_seen = round((now - min(fss)) / 86400.0, 2) if fss else None
        active_now = (
            1 if (any(m.get("in_stock") == 1 for m in members)
                  or (set(skus) & wl_active_skus)) else 0
        )
        recent_churn = 1 if (set(skus) & churn_skus) else 0
        ratings = [m.get("rating") for m in members if m.get("rating") is not None]
        avg_rating = round(statistics.fmean(ratings), 3) if ratings else None

        commercial = {
            "dpi": dpi,
            "store_count": store_count,
            "app_count": app_count,
            "days_since_first_seen": days_since_first_seen,
            "is_currently_active": active_now,
            "recent_churn_flag": recent_churn,
            "rating_avg": avg_rating,
        }
        rep_name = Counter(m.get("name", "") for m in members).most_common(1)[0][0]
        present_apps = sorted({m.get("app") for m in members})

        out[gid] = {
            "attribute_vector": attribute_vector,
            "commercial_features": commercial,
            "category": cat,
            "rep_name": rep_name,
            "member_count": len(members),
            "present_apps": present_apps,
            "flags": flags,
            "unit_price": up_val,
            "pack_value": pv_val,
        }
    return out


if __name__ == "__main__":
    # Offline self-test (no DB, no numpy).
    def mk(gid, app, store, name, cat, price, unit_price, pack_value,
           mp=False, in_stock=1):
        return dict(product_group_id=gid, app=app, store_id=store, name=name,
                    category=cat, price=price, unit_price=unit_price,
                    pack_value=pack_value, is_multipack=mp, in_stock=in_stock,
                    sku_key=f"{store}-{name}")

    rows = [
        # G1: two apps, ~59 ₹/L, 500ml
        mk("G1", "blinkit", "B1", "Amul 500ml", "Dairy", 29, 58.0, 500),
        mk("G1", "zepto", "Z1", "Amul 500 ml", "Dairy", 30, 60.0, 500),
        # G2: one app, 55 ₹/L, 1L  (different unit_price + pack within Dairy)
        mk("G2", "blinkit", "B1", "Amul 1L", "Dairy", 55, 55.0, 1000),
        # G3: no parsed pack -> no unit_price (must not crash, must flag)
        mk("G3", "blinkit", "B1", "Fresh Banana", "Produce", 40, None, None),
    ]

    vecs = build_product_vectors(rows)  # no db -> DB fields degrade to None/0
    assert len(vecs) == 3, list(vecs.keys())
    v1, v2, v3 = vecs["G1"], vecs["G2"], vecs["G3"]

    # attribute_vector is length 4
    assert len(v1["attribute_vector"]) == 4
    # z-scored unit_price differs between G1 (mean ~59) and G2 (55) in Dairy
    assert v1["attribute_vector"][0] != v2["attribute_vector"][0], (v1, v2)
    # z-scored pack differs (500 vs 1000)
    assert v1["attribute_vector"][1] != v2["attribute_vector"][1]
    # group with no unit_price -> imputation/missing flag, still length 4, no crash
    assert v3["flags"].get("unit_price_imputed") or v3["flags"].get("unit_price_missing")
    assert len(v3["attribute_vector"]) == 4
    # aggregation
    assert v1["commercial_features"]["app_count"] == 2
    assert v1["commercial_features"]["store_count"] == 2
    # vouchers excluded defensively
    rows.append(mk("GV", "blinkit", "B1", "Steam Gift Card", "Other", 500, None, None))
    vecs2 = build_product_vectors(rows)
    assert "GV" not in vecs2, "voucher must be excluded"

    print("[product_vectors] self-test OK")
