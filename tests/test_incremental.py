"""M11 incremental refresh — stdlib only."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import incremental as I

def test_watermark_and_plan():
    assert I.catalog_watermark(None) is None
    plan = I.plan_refresh([{"product_group_id":"G"}], db=None, force=True)
    assert plan["full_refresh_needed"] is True

def test_run():
    res = I.run_opportunity_refresh([{"product_group_id":"G","category":"C"}], db=None, force=False)
    assert res["stale"] is None or isinstance(res["stale"], bool)

if __name__ == "__main__":
    test_watermark_and_plan(); test_run()
    print("ok test_incremental")
