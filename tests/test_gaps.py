"""Unit tests for src.gaps.score_internal_gaps (M5 internal/attribute gaps).

Run:
    python3 -m pytest tests/test_gaps.py -q
  or:
    python3 tests/test_gaps.py
"""
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src import gaps as G


def _mk(gid, cat, name, up, pv, brand, app="blinkit"):
    return dict(product_group_id=gid, category=cat, name=name, app=app,
                store_id=app + "1", sku_key=app + "-" + name,
                unit_price=up, pack_value=pv, brand=brand, in_stock=1)


def _rows():
    rows = []
    for i in range(30):
        rows.append(_mk(f"S{i}", "Spices", f"Spice {i}", 10.0 + i * 0.1,
                       100.0 + i, "Generic"))
    rows.append(_mk("ISO", "Spices", "Rare Exotic Spice", 900.0, 50.0, "Exotic"))
    rows.append(_mk("R1", "Rare", "Odd Item 1", 20.0, 200.0, "X"))
    rows.append(_mk("R2", "Rare", "Odd Item 2", 25.0, 220.0, "X"))
    return rows


def test_attribute_outlier_flagged():
    gaps = G.score_internal_gaps(_rows(), min_n=25)
    ids = {g["product_group_id"] for g in gaps}
    assert "ISO" in ids


def test_guarded_category_excluded():
    gaps = G.score_internal_gaps(_rows(), min_n=25)
    ids = {g["product_group_id"] for g in gaps}
    assert "R1" not in ids and "R2" not in ids


def test_reason_codes_present():
    gaps = G.score_internal_gaps(_rows(), min_n=25)
    iso = next(g for g in gaps if g["product_group_id"] == "ISO")
    assert "attribute_outlier_vs_category" in iso["reason_codes"]
    assert "sparse_in_price_pack_space" in iso["reason_codes"]
    assert iso["internal_gap_score"] > 0.5


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
