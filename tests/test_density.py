"""Unit tests for src/density.compute_density / sparse_candidates (M4 guards).

Run:
    python3 -m pytest tests/test_density.py -q
  or:
    python3 tests/test_density.py
"""
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src import density as D


def _mk(gid, cat, name, app, up, pv, brand):
    return dict(product_group_id=gid, category=cat, name=name, app=app,
                store_id=app + "1", sku_key=app + "-" + name,
                unit_price=up, pack_value=pv, brand=brand, in_stock=1)


def _rows():
    rows = []
    for i in range(30):
        rows.append(_mk(f"S{i}", "Spices", f"Spice {i}", "blinkit",
                        10.0 + i * 0.1, 100.0 + i, "Generic"))
    rows.append(_mk("ISO", "Spices", "Rare Exotic Spice", "blinkit",
                    900.0, 50.0, "Exotic"))
    rows.append(_mk("R1", "Rare", "Odd Item 1", "blinkit", 20.0, 200.0, "X"))
    rows.append(_mk("R2", "Rare", "Odd Item 2", "zepto", 25.0, 220.0, "X"))
    return rows


def test_well_covered_not_flagged():
    by = {d["product_group_id"]: d for d in D.compute_density(_rows(), min_n=25)}
    assert not by["S0"]["insufficient_coverage"]


def test_isolated_group_is_sparse():
    by = {d["product_group_id"]: d for d in D.compute_density(_rows(), min_n=25)}
    iso = by["ISO"]
    assert iso["knn_distance"] is not None and iso["knn_distance"] > 0.5
    assert iso["neighbor_count"] == 0


def test_undercovered_category_flagged():
    by = {d["product_group_id"]: d for d in D.compute_density(_rows(), min_n=25)}
    assert by["R1"]["insufficient_coverage"] and by["R2"]["insufficient_coverage"]


def test_sparse_candidates_exclude_guarded():
    dens = D.compute_density(_rows(), min_n=25)
    cands = {c["product_group_id"] for c in D.sparse_candidates(dens)}
    assert "R1" not in cands and "R2" not in cands
    assert "ISO" in cands


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"ok  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    if failed:
        sys.exit(1)
    print(f"\nAll {len(tests)} tests passed")
