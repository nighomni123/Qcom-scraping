"""Unit tests for src/product_space.py (M1 normalized union + entity resolution).

Builds tiny throwaway sqlite files under a temp root and asserts:
  1. provenance round-trip: every normalized record can be traced back to its
     source inventory_catalog row -> raw_json.
  2. entity resolution: cross-app 500ml merge to ONE group; a 1L is its own
     group (pack-size veto); vouchers are excluded.
  3. deals.db catalog_snapshots union fallback (and no double-counting when the
     same sku already came from the rich inventory path).

Run:
    python3 -m unittest tests.test_product_space -v
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src import product_space as ps


_CATALOG_SCHEMA = """
CREATE TABLE inventory_catalog (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL,
    app TEXT, store_id TEXT, sku_key TEXT,
    name TEXT, price REAL, mrp REAL, in_stock INTEGER,
    url TEXT, collections TEXT, category TEXT,
    raw_json TEXT, raw_json_truncated INTEGER DEFAULT 0, raw_json_bytes INTEGER,
    UNIQUE(app, store_id, sku_key))
"""


def _make_inventory_db(root, app, rows):
    inv_dir = os.path.join(root, "inventory")
    os.makedirs(inv_dir, exist_ok=True)
    dbp = os.path.join(inv_dir, f"inventory_{app}.db")
    con = sqlite3.connect(dbp)
    con.execute(_CATALOG_SCHEMA)
    for r in rows:
        con.execute(
            "INSERT INTO inventory_catalog "
            "(ts,app,store_id,sku_key,name,price,mrp,in_stock,url,collections,category,"
            " raw_json,raw_json_truncated,raw_json_bytes) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (1.0, app, r["store_id"], r["sku_key"], r["name"], r["price"], r["mrp"],
             r["in_stock"], r["url"], r["collections"], r["category"],
             r["raw_json"], r.get("trunc", 0), r.get("bytes", 0)),
        )
    con.commit()
    con.close()
    return dbp


def _make_deals_db(root, snap_rows):
    dbp = os.path.join(root, "deals.db")
    con = sqlite3.connect(dbp)
    con.execute(
        "CREATE TABLE catalog_snapshots ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL,"
        " app TEXT, store_id TEXT, sku_key TEXT, name TEXT, price REAL,"
        " in_stock INTEGER, collections TEXT)"
    )
    for r in snap_rows:
        con.execute(
            "INSERT INTO catalog_snapshots (ts,app,store_id,sku_key,name,price,in_stock,collections)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (r["ts"], r["app"], r["store_id"], r["sku_key"], r["name"], r["price"],
             r["in_stock"], r["collections"]),
        )
    con.commit()
    con.close()
    return dbp


class TestProductSpace(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ps_test_")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_provenance_roundtrip(self):
        rows_in = [
            dict(store_id="B1", sku_key="k1", name="Amul Taaza Toned Milk 500ml",
                 price=29.0, mrp=33.0, in_stock=1, url="https://b/x",
                 collections="home,Milk", category="Dairy & Eggs",
                 raw_json=json.dumps({"id": "k1", "shelf": "milk"})),
            dict(store_id="B1", sku_key="k2", name="Britannia Gold Cake 1kg",
                 price=120.0, mrp=130.0, in_stock=1, url="https://b/y",
                 collections="home,Bakery", category="Bakery",
                 raw_json=json.dumps({"id": "k2", "shelf": "bakery"})),
        ]
        dbp = _make_inventory_db(self.root, "blinkit", rows_in)
        out = ps.load_product_space(apps=["blinkit"], root=self.root)

        self.assertEqual(len(out), 2, "both non-voucher rows should be returned")
        # Every normalized record traces back to its inventory_catalog row -> raw_json
        con = sqlite3.connect(f"file:{os.path.abspath(dbp)}?mode=ro", uri=True)
        try:
            for r in out:
                # provenance pointer is the abspath of the source inventory DB
                self.assertEqual(r["inventory_db"], os.path.abspath(dbp))
                # and the stored raw_json round-trips exactly
                src = con.execute(
                    "SELECT raw_json FROM inventory_catalog "
                    "WHERE app=? AND store_id=? AND sku_key=?",
                    (r["app"], r["store_id"], r["sku_key"]),
                ).fetchone()
                self.assertIsNotNone(src, "provenance: sku not found in source table")
                self.assertEqual(src[0], r["raw_json"], "raw_json mismatch vs source")
        finally:
            con.close()

    def test_entity_resolution_and_voucher_exclusion(self):
        rows_in = [
            # cross-app: same 500ml product -> ONE group
            dict(store_id="B1", sku_key="k1", name="Amul Taaza Toned Milk 500ml",
                 price=29.0, mrp=33.0, in_stock=1, url="https://b/x",
                 collections="home,Milk", category="Dairy & Eggs",
                 raw_json=json.dumps({"id": "k1"})),
            dict(store_id="Z1", sku_key="z9", name="Amul Taaza Milk 500 ml",
                 price=30.0, mrp=33.0, in_stock=1, url="https://z/x",
                 collections="home", category="Dairy & Eggs",
                 raw_json=json.dumps({"id": "z9"})),
            # different pack -> SEPARATE group (pack-size veto)
            dict(store_id="B1", sku_key="k2", name="Amul Taaza Toned Milk 1L",
                 price=55.0, mrp=60.0, in_stock=1, url="https://b/y",
                 collections="home,Milk", category="Dairy & Eggs",
                 raw_json=json.dumps({"id": "k2"})),
            # voucher -> excluded entirely
            dict(store_id="B1", sku_key="k3", name="Steam Wallet Gift Card 500",
                 price=500.0, mrp=500.0, in_stock=1, url="",
                 collections="home", category="Other",
                 raw_json=json.dumps({"id": "k3"})),
        ]
        _make_inventory_db(self.root, "blinkit", rows_in)
        # also drop a zepto copy of the same 500ml row to exercise cross-app merge
        _make_inventory_db(self.root, "zepto", [rows_in[1]])
        out = ps.load_product_space(apps=["blinkit", "zepto"], root=self.root)

        # voucher excluded
        self.assertNotIn("Gift Card", " ".join(r["name"] for r in out))

        g500 = [r for r in out if "500" in r["name"]]
        g1l = [r for r in out if "1L" in r["name"]]
        self.assertEqual(len({r["product_group_id"] for r in g500}), 1,
                         "cross-app 500ml SKUs must share one group")
        self.assertEqual(len({r["product_group_id"] for r in g1l}), 1,
                         "1L must be its own group")
        self.assertNotEqual(g500[0]["product_group_id"], g1l[0]["product_group_id"],
                            "pack-size veto must keep 500ml and 1L apart")

    def test_catalog_fallback_union(self):
        # No inventory DB for instamart -> deals.db catalog_snapshots supplies rows
        snap = [
            dict(ts=1.0, app="instamart", store_id="S1", sku_key="s1",
                 name="Tata Salt 1kg", price=25.0, in_stock=1, collections="home"),
        ]
        _make_deals_db(self.root, snap)
        out = ps.load_product_space(apps=["instamart"], root=self.root)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["source"], "catalog")
        self.assertEqual(out[0]["inventory_db"], os.path.abspath(os.path.join(self.root, "deals.db")))
        self.assertEqual(out[0]["name"], "Tata Salt 1kg")

    def test_no_double_count_when_both_sources(self):
        # Same (app,store,sku) present in BOTH inventory DB and deals.db.
        inv = [dict(store_id="B1", sku_key="k1", name="Amul Taaza Toned Milk 500ml",
                    price=29.0, mrp=33.0, in_stock=1, url="https://b/x",
                    collections="home", category="Dairy & Eggs",
                    raw_json=json.dumps({"id": "k1"}))]
        _make_inventory_db(self.root, "blinkit", inv)
        _make_deals_db(self.root, [dict(ts=1.0, app="blinkit", store_id="B1", sku_key="k1",
                                        name="Amul Taaza Toned Milk 500ml", price=29.0,
                                        in_stock=1, collections="home")])
        out = ps.load_product_space(apps=["blinkit"], root=self.root)
        self.assertEqual(len(out), 1, "same sku in both sources must not be double-counted")
        self.assertEqual(out[0]["source"], "inventory", "rich path must win over catalog fallback")


if __name__ == "__main__":
    unittest.main(verbosity=2)
