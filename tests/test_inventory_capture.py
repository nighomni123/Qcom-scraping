"""Incremental inventory capture (M1).

Regression test for the "stop midway loses everything" bug: with incremental
capture, a crawl that is interrupted before emitting its terminal summary must
still have persisted the product batches it streamed so far.

The crawler (tools/pw_catalog.js) now emits one {type:'batch'} JSON line per
visit. The adapter streams those on the main thread and the watchlist builder
flushes them to inventory_catalog every 2 visits (so a hard kill loses at most
~1 visit). This test simulates that path with a fake adapter: it calls
on_batch for 5 batches then returns [] (the live process died mid-sweep, so no
terminal summary ever arrives).
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.store import Store
from src.watchlist import WatchlistBuilder


class FakeAdapter:
    """Mimics the browser crawler streaming per-visit batches then dying."""

    def __init__(self, batches):
        self._batches = batches
        self.deep_sweep_calls = 0

    def deep_sweep(self, store_id, lat, lon, categories=0, terms=None,
                   deep_cats=False, skip_override=None, mirror_page_ms=None,
                   tabs=None, on_batch=None):
        self.deep_sweep_calls += 1
        for b in self._batches:
            if on_batch:
                on_batch(b)
        # Simulate the crawl dying before emitting its terminal summary.
        return [], {}


def _samples():
    # 5 visits; each harvests a fresh, non-overlapping SKU set (like
    # emitVisitBatch's per-visit delta).
    batches = []
    for v in range(5):
        batch = []
        for k in range(3):
            sku = f"S{v}-{k}"
            batch.append({
                "sku_key": sku,
                "name": f"Product {sku}",
                "collections": ["home"],
                "price": 10 + v,
                "mrp": 12 + v,
                "in_stock": 1,
                "url": f"https://example/{sku}",
                "raw": {"id": sku, "v": v},
            })
        batches.append(batch)
    return batches


class InventoryCaptureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="invcap_")
        self.deals = os.path.join(self.tmp, "deals.db")
        self.inv = os.path.join(self.tmp, "inventory_test.db")
        self.db = Store(self.deals)
        self.inv_db = Store(self.inv)
        self.cfg = {"demand": {}}

    def tearDown(self):
        for p in (self.deals, self.inv):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass
        try:
            os.rmdir(self.tmp)
        except OSError:
            pass

    def _count(self):
        return self.inv_db.conn.execute(
            "SELECT COUNT(*) FROM inventory_catalog").fetchone()[0]

    def test_incremental_flush_every_two_visits(self):
        batches = _samples()
        fake = FakeAdapter(batches)
        wb = WatchlistBuilder(self.cfg, self.db, inventory_db=self.inv_db)
        wb._make_adapter = lambda app: fake
        wb.build_one_store("blinkit", "999", 19.0, 72.8, catalog=True)
        # 5 batches streamed; the builder flushes at visit 2 and 4 -> batches
        # 0..3 persisted (12 rows). Batch 4 stays buffered and is lost on the
        # simulated interrupt (the run ends before the final agg upsert), which
        # is the accepted "~1 visit" loss on a hard kill.
        self.assertEqual(self._count(), 12)
        self.assertEqual(fake.deep_sweep_calls, 1)

    def test_full_run_persists_all_rows_idempotently(self):
        # When the crawl completes, the terminal summary returns the full
        # product set; the final agg upsert must persist everything (the
        # buffered batches are a redundant safety net).
        batches = _samples()

        class FullAdapter(FakeAdapter):
            def deep_sweep(self, *a, **k):
                on_batch = k.get("on_batch")
                for b in self._batches:
                    if on_batch:
                        on_batch(b)
                # Terminal summary returns the complete deduped set.
                allp = [p for b in self._batches for p in b]
                return allp, {}

        wb = WatchlistBuilder(self.cfg, self.db, inventory_db=self.inv_db)
        wb._make_adapter = lambda app: FullAdapter(batches)
        wb.build_one_store("blinkit", "999", 19.0, 72.8, catalog=True)
        self.assertEqual(self._count(), 15)  # all 5 batches * 3 SKUs


if __name__ == "__main__":
    unittest.main()
