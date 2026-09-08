"""
M9 — Temporal / emerging-segment + stability (Phase 14 + 15).
Pure stdlib; db=None degrades to count proxy; additive (no schema changes).
"""
from __future__ import annotations


def _history_counts(db, window_days=14):
    """Per-category distinct-SKU counts for the two most recent sweeps.

    catalog_snapshots has no category column; category labels live in the
    `collections` CSV, so parse rows in Python. Returns (counts_old, counts_new)
    dicts, or (None, None) when history is unavailable.
    """
    import time
    if db is None or not hasattr(db, "conn"):
        return None, None
    try:
        now = time.time()
        cutoff = now - window_days * 86400
        ts_rows = db.conn.execute(
            "SELECT DISTINCT ts FROM catalog_snapshots WHERE ts >= ? ORDER BY ts",
            (cutoff,),
        ).fetchall()
        ts_vals = sorted(r[0] for r in ts_rows if r[0] is not None)
        if len(ts_vals) < 1:
            return None, None
        recent = ts_vals[-2:]  # oldest, newest (or just newest twice)

        def counts_at(ts):
            from collections import Counter
            c = Counter()
            for (sku, coll) in db.conn.execute(
                "SELECT sku_key, collections FROM catalog_snapshots WHERE ts = ?",
                (ts,),
            ).fetchall():
                if not sku:
                    continue
                cat = (coll or "").split(",")[0].strip()
                if not cat:
                    continue  # rows with empty category: skipped, not fabricated
                c[cat] += 1
            return c

        if len(recent) == 1:
            return None, counts_at(recent[0])
        return counts_at(recent[0]), counts_at(recent[1])
    except Exception:
        return None, None


def analyze_temporal(rows, db=None, window_days=14):
    """Classify temporal patterns for categories / product groups.

    Inputs: rows (product_space union) and optional store (db) for
    catalog_snapshots sweep history. Returns list of emission dicts.
    When db is not None, classifies from real per-sweep SKU counts
    (growing → emerging_segment, shrinking → declining_segment); the
    in-memory count proxy is used ONLY when db is None.
    """
    out = []
    if rows is None or len(rows) == 0:
        return out
    if db is not None:
        old, new = _history_counts(db, window_days)
        if new is not None:
            for cat in sorted(set(old or {}) | set(new)):
                n_new = new.get(cat, 0)
                n_old = (old or {}).get(cat, n_new)
                if n_new > n_old:
                    kind, conf = "emerging_segment", 0.75
                elif n_new < n_old:
                    kind, conf = "declining_segment", 0.75
                elif n_new >= 10:
                    kind, conf = "persistent", 0.8
                else:
                    kind, conf = "temporary_gap", 0.55
                out.append({
                    "kind": kind,
                    "category": cat,
                    "count": n_new,
                    "evidence": f"sweep counts {n_old}->{n_new} (catalog_snapshots history)",
                    "confidence": conf,
                })
            return out
        # fall through to proxy when history unavailable
    from collections import Counter
    cat_counts = Counter(r.get("category") or "" for r in rows)
    for cat, n in sorted(cat_counts.items()):
        if not cat:
            continue  # ponytail: ceiling=count proxy, upgrade=real sweep history; empty categories skipped, never classified
        # ponytail: ceiling=in-memory count proxy (n<10 emerging), upgrade=catalog_snapshots sweep history (db path above)
        kind = "emerging_segment" if n < 10 else "persistent"
        out.append({
            "kind": kind,
            "category": cat,
            "count": n,
            "evidence": f"category member count = {n} (approx from union rows; full temporal needs catalog_snapshots sweep history, station db not loaded)",
            "confidence": 0.6 if kind == "emerging_segment" else 0.8,
        })
    for cat, n in sorted(cat_counts.items()):
        if n >= 25 or n < 10 or not cat:
            continue
        out.append({
            "kind": "temporary_gap",
            "category": cat,
            "count": n,
            "evidence": f"sparse category (count={n}) — temporary until sweep confirms",
            "confidence": 0.55,
        })
    seen = {}
    unique = []
    for o in out:
        k = (o["kind"], o["category"])
        if k in seen:
            continue
        seen[k] = True
        unique.append(o)
    return unique


def stability_report(rows=None, db=None, min_n=25):
    """Phase 15: stability / robustness flags for opportunity candidates.
    Returns per-category dict. Unmeasured fields are None; temporal and
    coverage stability are measured from sweep history when db is given.
    """
    # ponytail: ceiling=static placeholders, upgrade=PCA-vs-UMAP + multi-seed kNN agreement
    out = {}
    if rows is None or len(rows) == 0:
        return out
    measured = {}
    if db is not None:
        old, new = _history_counts(db)
        if new is not None:
            for cat in set(old or {}) | set(new):
                present_both = old is not None and cat in old and cat in new
                measured[cat] = (1.0 if present_both else 0.5)
    categories = {r.get("category") for r in rows if r.get("category")}
    for cat in categories:
        t = measured.get(cat, 1.0 if db is None else 0.5)
        out[cat] = {
            "projection_stability": None,
            "neighbor_stability": None,
            "temporal_stability": t,
            "coverage_stability": t,
            "stability_score": None,
            "downgrade": None,
            "note": "projection/neighbor stability unmeasured (needs PCA-vs-UMAP + multi-seed kNN rerun)",
        }
    return out


if __name__ == "__main__":
    # Offline self-test
    rows = [
        {"product_group_id": "G1", "category": "Bev", "name": "X"},
        {"product_group_id": "G2", "category": "Bev", "name": "Y"},
        {"product_group_id": "G3", "category": "Dairy", "name": "Z"},
    ]
    t = analyze_temporal(rows)
    assert any(o["kind"] == "emerging_segment" and o["category"] == "Dairy" for o in t), "emerging not detected"
    s = stability_report(rows)
    assert s["Bev"]["temporal_stability"] == 1.0 and s["Bev"]["stability_score"] is None
    assert analyze_temporal([]) == []
    # real-history path: growing Bev 3->8, shrinking Dairy 8->3
    import sqlite3, time as _t
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE catalog_snapshots (ts REAL, sku_key TEXT, collections TEXT)")
    now = _t.time()
    for i in range(3):
        conn.execute("INSERT INTO catalog_snapshots VALUES (?,?,?)", (now - 1000, f"b{i}", "Bev"))
    for i in range(8):
        conn.execute("INSERT INTO catalog_snapshots VALUES (?,?,?)", (now, f"b{i}", "Bev"))
    for i in range(8):
        conn.execute("INSERT INTO catalog_snapshots VALUES (?,?,?)", (now - 1000, f"d{i}", "Dairy"))
    for i in range(3):
        conn.execute("INSERT INTO catalog_snapshots VALUES (?,?,?)", (now, f"d{i}", "Dairy"))

    class _Db:
        pass
    _d = _Db()
    _d.conn = conn
    t2 = analyze_temporal(rows, db=_d)
    assert any(o["kind"] == "emerging_segment" and o["category"] == "Bev" for o in t2), t2
    assert any(o["kind"] == "declining_segment" and o["category"] == "Dairy" for o in t2), t2
    print("[temporal] self-test OK")
