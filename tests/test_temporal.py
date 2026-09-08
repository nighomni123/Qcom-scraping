"""M9 temporal stability — stdlib only."""
import sys, os, sqlite3, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import temporal as T

def _scratch_db(series):
    """series: {category: [count_per_sweep]}. Returns db wrapper."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE catalog_snapshots (ts REAL, sku_key TEXT, collections TEXT)")
    now = time.time()
    for cat, counts in series.items():
        for sweep, n in enumerate(counts):
            for i in range(n):
                conn.execute("INSERT INTO catalog_snapshots VALUES (?,?,?)",
                             (now - (len(counts) - sweep) * 1000, f"{cat}{i}", cat))
    class _Db: pass
    d = _Db(); d.conn = conn; return d

def test_emerging():
    rows = [{"category":"X","name":f"a{i}"} for i in range(3)]
    out = T.analyze_temporal(rows)  # proxy path, db=None
    assert any(o["kind"]=="emerging_segment" and o["category"]=="X" for o in out)

def test_real_history_growth():
    db = _scratch_db({"Grow": [3, 8, 15, 27, 43], "Shrink": [43, 27, 15]})
    rows = [{"category": "Grow"}, {"category": "Shrink"}]
    out = T.analyze_temporal(rows, db=db)
    by_cat = {o["category"]: o for o in out}
    assert by_cat["Grow"]["kind"] == "emerging_segment", by_cat
    assert by_cat["Shrink"]["kind"] == "declining_segment", by_cat

def test_stability_net():
    s = T.stability_report([{"category":"C"}])
    assert s["C"]["temporal_stability"] == 1.0
    assert s["C"]["projection_stability"] is None
    assert s["C"]["stability_score"] is None

def test_offline_empty():
    assert T.analyze_temporal([])==[]
    assert not T.stability_report()

if __name__ == "__main__":
    test_emerging(); test_real_history_growth(); test_stability_net(); test_offline_empty()
    print("ok test_temporal")
