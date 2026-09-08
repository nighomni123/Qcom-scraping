"""M9 temporal stability — stdlib only."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import temporal as T

def test_emerging():
    rows = [{"category":"X","name":f"a{i}"} for i in range(3)]
    out = T.analyze_temporal(rows)
    assert any(o["kind"]=="emerging_segment" and o["category"]=="X" for o in out)

def test_stability_net():
    s = T.stability_report([{"category":"C"}])
    assert s.get("C",{}).get("stability_score",0)>0

def test_offline_empty():
    assert T.analyze_temporal([])==[]
    assert not T.stability_report()

if __name__ == "__main__":
    test_emerging(); test_stability_net(); test_offline_empty()
    print("ok test_temporal")
