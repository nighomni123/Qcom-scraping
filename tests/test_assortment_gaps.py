"""
tests/test_assortment_gaps.py — assertions on detect_assortment_gaps.

Run via:  python3 -m pytest tests/test_assortment_gaps.py -q
  or:      python3 tests/test_assortment_gaps.py
"""
import sys
from collections import defaultdict

# make the repo root importable whether run by pytest or standalone
import os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.assortment_gaps import detect_assortment_gaps


def _row(gid, app, store_id, name, category="Grocery"):
    return {"product_group_id": gid, "app": app, "store_id": store_id,
            "name": name, "category": category, "in_stock": 1}


def _build_rows():
    return [
        # G1: blinkit + zepto only -> gap missing instamart
        _row("G1", "blinkit", "B1", "Amul Taaza Milk 500ml"),
        _row("G1", "zepto", "Z1", "Amul Taaza Milk 500 ml"),
        # G2: all three -> NOT a gap
        _row("G2", "blinkit", "B1", "Tata Salt 1kg"),
        _row("G2", "zepto", "Z1", "Tata Salt 1kg"),
        _row("G2", "instamart", "I1", "Tata Salt 1kg"),
        # G3: instamart only -> gap missing blinkit + zepto
        _row("G3", "instamart", "I1", "Rare Local Snack"),
        # G4: only bigbasket (not tracked) -> NOT a gap
        _row("G4", "bigbasket", "BB1", "Bigbasket Exclusive Tea"),
    ]


def test_g1_present_on_two_missing_one():
    gaps = detect_assortment_gaps(_build_rows())
    by = {g["product_group_id"]: g for g in gaps}
    assert "G1" in by
    assert by["G1"]["present_apps"] == ["blinkit", "zepto"]
    assert by["G1"]["missing_apps"] == ["instamart"]


def test_g2_present_on_all_not_a_gap():
    gaps = detect_assortment_gaps(_build_rows())
    by = {g["product_group_id"]: g for g in gaps}
    assert "G2" not in by


def test_g3_present_on_one_missing_two():
    gaps = detect_assortment_gaps(_build_rows())
    by = {g["product_group_id"]: g for g in gaps}
    assert "G3" in by
    assert by["G3"]["present_apps"] == ["instamart"]
    assert set(by["G3"]["missing_apps"]) == {"blinkit", "zepto"}


def test_g4_only_non_tracked_not_a_gap():
    gaps = detect_assortment_gaps(_build_rows())
    by = {g["product_group_id"]: g for g in gaps}
    assert "G4" not in by


def test_confidence_in_range():
    for g in detect_assortment_gaps(_build_rows()):
        assert 0.0 <= g["confidence"] <= 1.0


def test_custom_tracked_apps():
    # when tracking only blinkit+zepto: G1 is present on BOTH tracked apps
    # (-> not a gap), and G3 (instamart only) is outside the tracked set
    # (-> not localizable as an assortment gap here).
    rows = _build_rows()
    gaps = detect_assortment_gaps(rows, tracked_apps=("blinkit", "zepto"))
    by = {g["product_group_id"]: g for g in gaps}
    assert "G1" not in by        # present on all tracked apps -> not a gap
    assert "G3" not in by        # instamart not in tracked set -> not localizable


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
