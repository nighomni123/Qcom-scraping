"""Unit tests for src.opportunities.score_opportunities (M6 scoring + validation).

Run:
    python3 -m pytest tests/test_opportunities.py -q
  or:
    python3 tests/test_opportunities.py
"""
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src import opportunities as O


def _mk(gid, cat, name, up, pv, brand, app="blinkit"):
    return dict(product_group_id=gid, category=cat, name=name, app=app,
                store_id=app + "1", sku_key=app + "-" + name,
                unit_price=up, pack_value=pv, brand=brand, in_stock=1)


def _rows():
    rows = []
    for i in range(30):
        rows.append(_mk(f"S{i}", "CatA", f"Spice {i}", 10.0 + i * 0.1,
                       100.0 + i, "Generic"))
    rows.append(_mk("SX", "CatA", "Instamart Cover Spice", 15.0, 150.0,
                    "Generic", "instamart"))
    rows.append(_mk("GA", "CatA", "Rare Exotic Spice", 900.0, 50.0, "Exotic", "blinkit"))
    rows.append(_mk("GA", "CatA", "Rare Exotic Spice", 905.0, 50.0, "Exotic", "zepto"))
    rows.append(_mk("R1", "Rare", "Odd Item 1", 20.0, 200.0, "X"))
    rows.append(_mk("R2", "Rare", "Odd Item 2", 25.0, 220.0, "X"))
    return rows


def test_gap_types_and_score():
    opps = O.score_opportunities(_rows(), min_n=25)
    ids = {o["product_group_id"] for o in opps}
    assert "GA" in ids
    ga = next(o for o in opps if o["product_group_id"] == "GA")
    assert sorted(ga["gap_types"]) == ["assortment", "internal"]
    assert ga["score"] > 0


def test_guarded_category_excluded():
    opps = O.score_opportunities(_rows(), min_n=25)
    ids = {o["product_group_id"] for o in opps}
    assert "R1" not in ids and "R2" not in ids


def test_score_is_product_of_breakdown():
    opps = O.score_opportunities(_rows(), min_n=25)
    ga = next(o for o in opps if o["product_group_id"] == "GA")
    expected = round(ga["breakdown"]["gap_strength"] * ga["breakdown"]["dpi"] *
                     ga["breakdown"]["coverage"] * ga["breakdown"]["churn"], 4)
    assert ga["score"] == expected


def test_validation_codes_present():
    opps = O.score_opportunities(_rows(), min_n=25)
    ga = next(o for o in opps if o["product_group_id"] == "GA")
    assert isinstance(ga["validation_reason_codes"], list)


def test_persistence_round_trip():
    import sqlite3, tempfile, os
    opps = O.score_opportunities(_rows(), min_n=25)
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    try:
        conn = sqlite3.connect(tmp.name)

        class _C:
            def __init__(self, conn):
                self.conn = conn

        db = _C(conn)
        n = O.persist_opportunities(db, opps)
        assert n == len(opps), f"persisted {n} != {len(opps)}"
        back = O.load_latest_opportunities(db, limit=50)
        assert len(back) == len(opps), "round-trip count mismatch"
        ga = next(o for o in back if o["product_group_id"] == "GA")
        orig = next(o for o in opps if o["product_group_id"] == "GA")
        assert ga["score"] == orig["score"], "score not preserved"
        assert ga["gap_types"] == orig["gap_types"], "gap_types lost"
        assert isinstance(ga["provenance"], dict), "provenance lost"
        # a second snapshot appends; latest snapshot wins
        O.persist_opportunities(db, opps[:3])
        assert len(O.load_latest_opportunities(db, limit=50)) == 3
        conn.close()
    finally:
        os.unlink(tmp.name)


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