"""M11 incremental refresh — stdlib only."""
import sys, os, sqlite3, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import incremental as I

def _scratch_db(now):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE catalog_snapshots (ts REAL, sku_key TEXT, collections TEXT)")
    conn.execute("CREATE TABLE catalog_events (ts REAL, sku_key TEXT, kind TEXT)")
    for sku in ("a", "b"):
        conn.execute("INSERT INTO catalog_snapshots VALUES (?,?,?)", (now - 1000, sku, "C"))
    conn.execute("INSERT INTO catalog_snapshots VALUES (?,?,?)", (now, "c", "C"))
    conn.execute("INSERT INTO catalog_events VALUES (?,?,?)", (now, "d", "new"))
    class _Db: pass
    d = _Db(); d.conn = conn; return d

def test_watermark_and_plan():
    assert I.catalog_watermark(None) is None
    plan = I.plan_refresh([{"product_group_id":"G"}], db=None, force=True)
    assert plan["full_refresh_needed"] is True
    assert plan["stages"]["full_refresh"] is True

def test_force_branch():
    rows = [{"product_group_id":"G1","category":"C","name":"X","app":"blinkit"}]
    res = I.run_opportunity_refresh(rows, db=None, force=True)
    assert res["stale"] is False and isinstance(res["opportunities"], list)

def test_fresh_watermark_stale_readback():
    now = time.time()
    db = _scratch_db(now)
    rows = [{"product_group_id": "G9", "category": "C", "name": "X", "app": "blinkit"}]
    changed = I.products_changed_since(db, now - 500)
    assert set(changed) == {"c", "d"}, changed
    assert I.products_changed_since(None, now) is None
    seeded = I.run_opportunity_refresh(rows, db=db, force=True)
    assert seeded["stale"] is False
    res = I.run_opportunity_refresh(rows, db=db, force=False)
    assert res["stale"] is True
    assert res["opportunities"] == seeded["opportunities"]

if __name__ == "__main__":
    test_watermark_and_plan(); test_force_branch(); test_fresh_watermark_stale_readback()
    print("ok test_incremental")
