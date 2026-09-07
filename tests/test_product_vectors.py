"""Unit tests for src/product_vectors.build_product_vectors (M2 vectors).

Builds a tiny synthetic row list (no DB) and asserts:
  1. attribute_vector is length 4 and z-scored per category (G1 vs G2 differ).
  2. a group with no parsed pack/unit_price is flagged, never crashes, never 0.
  3. store/app aggregation is correct.
  4. vouchers are excluded defensively.

Run:
    python3 -m pytest tests/test_product_vectors.py -q
  or:
    python3 tests/test_product_vectors.py
"""
import sys

import os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src import product_vectors as pv


def _mk(gid, app, store, name, cat, price, unit_price, pack_value,
        mp=False, in_stock=1):
    return dict(product_group_id=gid, app=app, store_id=store, name=name,
                category=cat, price=price, unit_price=unit_price,
                pack_value=pack_value, is_multipack=mp, in_stock=in_stock,
                sku_key=f"{store}-{name}")


def _rows():
    return [
        _mk("G1", "blinkit", "B1", "Amul 500ml", "Dairy", 29, 58.0, 500),
        _mk("G1", "zepto", "Z1", "Amul 500 ml", "Dairy", 30, 60.0, 500),
        _mk("G2", "blinkit", "B1", "Amul 1L", "Dairy", 55, 55.0, 1000),
        _mk("G3", "blinkit", "B1", "Fresh Banana", "Produce", 40, None, None),
    ]


def test_attribute_vector_shape_and_zscore():
    vecs = pv.build_product_vectors(_rows())
    assert len(vecs) == 3
    v1, v2 = vecs["G1"], vecs["G2"]
    assert len(v1["attribute_vector"]) == 4
    # within-category z-scores must differ for unit_price and pack
    assert v1["attribute_vector"][0] != v2["attribute_vector"][0]
    assert v1["attribute_vector"][1] != v2["attribute_vector"][1]


def test_missing_unit_price_is_flagged_not_zero():
    vecs = pv.build_product_vectors(_rows())
    v3 = vecs["G3"]
    assert v3["flags"].get("unit_price_imputed") or v3["flags"].get("unit_price_missing")
    assert len(v3["attribute_vector"]) == 4
    # unit_price must never be a fabricated 0
    assert v3["unit_price"] is None


def test_aggregation():
    vecs = pv.build_product_vectors(_rows())
    assert vecs["G1"]["commercial_features"]["app_count"] == 2
    assert vecs["G1"]["commercial_features"]["store_count"] == 2
    assert vecs["G1"]["commercial_features"]["is_currently_active"] == 1
    assert vecs["G2"]["commercial_features"]["app_count"] == 1


def test_voucher_excluded():
    rows = _rows() + [_mk("GV", "blinkit", "B1", "Steam Gift Card",
                          "Other", 500, None, None)]
    vecs = pv.build_product_vectors(rows)
    assert "GV" not in vecs


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
