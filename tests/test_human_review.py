"""M10 human review — stdlib only."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import human_review as H

def test_brief():
    brief = H.build_brief({"rep_name":"R","category":"C","score":0.3,"breakdown":{"gap_strength":0.3,"dpi":0.5,"coverage":1,"churn":1},"gap_types":["assortment"]})
    assert "Opportunity:" in brief

def test_taxonomy():
    cats = H.review_categories()
    assert "Strong opportunity" in cats

def test_review():
    assert H.submit_review(None,"G","Strong opportunity")
    try:
        H.submit_review(None,"G","NotReal")
        assert False
    except ValueError:
        pass

if __name__ == "__main__":
    test_brief(); test_taxonomy(); test_review()
    print("ok test_human_review")
