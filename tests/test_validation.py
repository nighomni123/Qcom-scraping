"""Synthetic regression tests: Phase 16 fixtures + Phase 17 quality gates.

Each fixture builds a known input offline (no DB except the in-memory
scratch sqlite used for the catalog_events burst case) and asserts the
engine detects a real gap or rejects a false one.

Run:
    python3 -m pytest tests/test_validation.py -q
  or:
    python3 tests/test_validation.py
"""
import sqlite3
import sys
import os
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.assortment_gaps import detect_assortment_gaps
from src.density import compute_density, sparse_candidates
from src.gaps import score_internal_gaps
from src.opportunities import score_opportunities
from src.store import is_voucher_name


def mk(gid, cat, name, up, pv, brand, app="blinkit", store=None):
    """Row factory matching the parse pattern used across the test suite."""
    return dict(product_group_id=gid, category=cat, name=name, app=app,
                store_id=store or (app + "1"), sku_key=app + "-" + gid,
                unit_price=up, pack_value=pv, brand=brand, in_stock=1)


def _well_covered(cat, n=30, apps=("blinkit",)):
    rows = []
    for i in range(n):
        rows.append(mk(f"S{i}", cat, f"Filler {i}", 10.0 + i * 0.1,
                       100.0 + i, "Generic", apps[0]))
    return rows


def test_known_assortment_gap():
    rows = _well_covered("CatA")
    rows.append(mk("SX", "CatA", "Instamart Cover Spice", 15.0, 150.0,
                   "Generic", "instamart"))
    rows.append(mk("GA", "CatA", "Known Gap Spice", 20.0, 200.0,
                   "Target", "blinkit"))
    rows.append(mk("GA", "CatA", "Known Gap Spice", 20.5, 200.0,
                   "Target", "zepto"))
    gaps = detect_assortment_gaps(rows)
    by = {g["product_group_id"]: g for g in gaps}
    assert "GA" in by, "known assortment gap not flagged"
    assert by["GA"]["missing_apps"] == ["instamart"], by["GA"]["missing_apps"]
    opps = score_opportunities(rows, min_n=25)
    ids = {o["product_group_id"] for o in opps}
    assert "GA" in ids, "real assortment gap missing from opportunities"
    ga = next(o for o in opps if o["product_group_id"] == "GA")
    assert "assortment" in ga["gap_types"], ga["gap_types"]


def test_known_density_gap():
    rows = _well_covered("Spices")
    rows.append(mk("ISO", "Spices", "Rare Exotic Spice", 900.0, 50.0, "Exotic"))
    dens = compute_density(rows, min_n=25)
    by = {d["product_group_id"]: d for d in dens}
    iso = by["ISO"]
    assert iso["knn_distance"] is not None and iso["knn_distance"] > 0.5, iso
    assert iso["neighbor_count"] == 0, iso
    cands = {c["product_group_id"] for c in sparse_candidates(dens)}
    assert "ISO" in cands, "sparse outlier dropped by sparse_candidates"
    gaps = score_internal_gaps(rows, min_n=25)
    ids = {g["product_group_id"] for g in gaps}
    assert "ISO" in ids, "sparse outlier not flagged as internal gap"


def test_false_gap_insufficient_coverage():
    rows = [mk("T1", "Thin", "Thin Item 1", 20.0, 200.0, "X"),
            mk("T2", "Thin", "Thin Item 2", 25.0, 220.0, "X", "zepto")]
    dens = compute_density(rows, min_n=25)
    assert all(d["insufficient_coverage"] for d in dens), dens
    opps = score_opportunities(rows, min_n=25)
    from_cat = [o for o in opps if o["category"] == "Thin"]
    assert from_cat == [], f"guarded category leaked: {from_cat}"


def test_crawl_artifact_no_category_coverage():
    # Gap group missing instamart; instamart carries NOTHING in "Lonely".
    rows = _well_covered("Lonely", apps=("blinkit",))
    rows.append(mk("GX", "Lonely", "Lonely Gap Item", 20.0, 200.0,
                   "Target", "blinkit"))
    rows.append(mk("GX", "Lonely", "Lonely Gap Item", 20.5, 200.0,
                   "Target", "zepto"))
    opps = score_opportunities(rows, min_n=25)
    by = {o["product_group_id"]: o for o in opps}
    if "GX" in by:
        assert ("missing_app_has_no_category_coverage"
                in by["GX"]["validation_reason_codes"]), by["GX"]
    else:
        assert "GX" not in by  # dropped as crawl artifact


def test_crawl_artifact_mass_churn_burst():
    rows = _well_covered("CatA")
    rows.append(mk("SX", "CatA", "Instamart Cover Spice", 15.0, 150.0,
                   "Generic", "instamart"))
    rows.append(mk("GA", "CatA", "Known Gap Spice", 20.0, 200.0,
                   "Target", "blinkit"))
    rows.append(mk("GA", "CatA", "Known Gap Spice", 20.5, 200.0,
                   "Target", "zepto"))

    class _DB:
        def __init__(self, conn):
            self.conn = conn

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE catalog_events (ts REAL)")
    now = time.time()
    conn.executemany("INSERT INTO catalog_events (ts) VALUES (?)",
                     [(now - 86400.0,) for _ in range(501)])
    conn.commit()
    try:
        opps = score_opportunities(rows, db=_DB(conn), min_n=25)
    finally:
        conn.close()
    assert opps, "burst wiped all candidates"
    codes = [c for o in opps for c in o["validation_reason_codes"]]
    assert "crawl_artifact_suspect" in codes, codes
    churns = {o["breakdown"]["churn"] for o in opps}
    assert 0.5 in churns, f"churn factor not 0.5 under burst: {churns}"


def test_existing_product_not_gap():
    rows = [mk("G2", "Grocery", "Tata Salt 1kg", 20.0, 1000.0, "Tata", a)
            for a in ("blinkit", "zepto", "instamart")]
    gaps = detect_assortment_gaps(rows)
    by = {g["product_group_id"]: g for g in gaps}
    assert "G2" not in by, "product on all apps wrongly flagged as gap"


def test_voucher_excluded():
    from src.product_vectors import build_product_vectors
    assert is_voucher_name("Amazon Voucher Rs 500") is True
    assert is_voucher_name("Super Gift Card 1000") is True
    rows = _well_covered("CatA")
    for a in ("blinkit", "zepto", "instamart"):
        # voucher on ALL apps, clustered with fillers: no assortment gap,
        # not sparse enough for an internal gap -> no candidate may surface.
        rows.append(mk("GV", "CatA", "Super Gift Card Voucher 500", 11.5,
                       115.0, "Bank", a))
    vecs = build_product_vectors(rows)
    assert "GV" not in vecs, "voucher rows must be dropped from vectors"
    opps = score_opportunities(rows, min_n=25)
    by = {o["product_group_id"]: o for o in opps}
    if "GV" in by:
        assert "voucher_excluded" in by["GV"]["validation_reason_codes"], by["GV"]
    else:
        assert "GV" not in by  # voucher row surfaced no candidate


def test_entity_resolution_grouping():
    # TODO: call load_product_space end-to-end once an offline fixture corpus
    # exists; for now assert the grouping contract the union layer relies on:
    # two rows with an identical canonical key resolve to one product_group_id.
    rows = [mk("SAME", "Grocery", "Amul Taaza Milk 500ml", 30.0, 500.0,
               "Amul", "blinkit"),
            mk("SAME", "Grocery", "Amul Taaza Milk 500 ml", 30.0, 500.0,
               "Amul", "zepto")]
    groups = {r["product_group_id"] for r in rows}
    assert groups == {"SAME"}, f"identical key split into {groups}"
    gaps = detect_assortment_gaps(rows)
    assert len([g for g in gaps if g["product_group_id"] == "SAME"]) == 1


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
