"""M10 human review — stdlib only."""
import sys, os, sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import human_review as H

def test_brief():
    brief = H.build_brief({"rep_name":"R","category":"C","score":None,"breakdown":{"gap_strength":0.3,"dpi":0.5,"coverage":1,"churn":1},"gap_types":["assortment"]})
    assert "Opportunity:" in brief
    assert "(no LLM interpretation" in brief
    stub = H.build_brief({"rep_name":"R","score":0.3}, llm=object())
    assert "not yet wired" in stub

def test_taxonomy():
    assert len(H.review_categories()) == 6
    assert "Strong opportunity" in H.review_categories()

def test_roundtrip():
    db = sqlite3.connect(":memory:")
    H.submit_review(db, "G", "Strong opportunity", note="looks real")
    revs = H.reviews_for(db, "G")
    assert len(revs) == 1 and revs[0]["decision"] == "Strong opportunity"
    opps = H.apply_review_feedback(db, [{"product_group_id": "G"}, {"product_group_id": "Gx"}])
    assert opps[0]["human_decision"] == "Strong opportunity"
    assert "human_note" not in opps[1]

def test_review():
    try:
        H.submit_review(None, "G", "Strong opportunity")
        assert False, "db=None must raise"
    except ValueError:
        pass
    try:
        H.submit_review(sqlite3.connect(":memory:"), "G", "NotReal")
        assert False
    except ValueError:
        pass

if __name__ == "__main__":
    test_brief(); test_taxonomy(); test_roundtrip(); test_review()
    print("ok test_human_review")
