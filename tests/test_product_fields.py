"""Unit tests for src/product_fields.py (grocery name field parser).

These are the smallest runnable checks that fail if the parser logic breaks —
no frameworks, no fixtures, stdlib unittest only. Run:
    python3 -m unittest tests.test_product_fields -v
"""
import unittest

from src import product_fields as pf


class TestParseName(unittest.TestCase):
    """Mirror the curated SELFTEST_CASES and assert exact (brand, value, unit, mp)."""

    def test_selftest_cases(self):
        for name, expected in pf.SELFTEST_CASES:
            got = pf.parse_name(name)
            exp_brand, exp_val, exp_unit, exp_mp = expected
            self.assertEqual(got["brand"], exp_brand, f"brand mismatch: {name!r}")
            self.assertEqual(got["pack_value"], exp_val, f"pack_value mismatch: {name!r}")
            self.assertEqual(got["pack_unit"], exp_unit, f"pack_unit mismatch: {name!r}")
            self.assertEqual(got["is_multipack"], exp_mp, f"is_multipack mismatch: {name!r}")

    def test_no_pack_is_not_zero_unit_price(self):
        # The M1 invariant: a name with no detectable pack must yield
        # unit_price=None and parse_status='no_pack' — never a fabricated 0 which
        # would corrupt every unit-price axis later.
        r = pf.parse_name("Baker's Loaf Multigrain Bread", price=50)
        self.assertIsNone(r["pack_value"])
        self.assertIsNone(r["unit_price"])
        self.assertEqual(r["parse_status"], "no_pack")

    def test_missing_price_keeps_unit_price_none(self):
        # Even with a pack detected, a missing price cannot yield a unit_price.
        r = pf.parse_name("Amul Taaza Toned Milk 500ml")  # no price arg
        self.assertEqual(r["pack_value"], 500.0)
        self.assertIsNone(r["unit_price"])

    def test_unit_price_computed_when_price_present(self):
        r = pf.parse_name("Amul Taaza Toned Milk 500ml", price=29.0)
        # 29 / 0.5L => 58 /L
        self.assertEqual(r["unit_price"], 58.0)
        self.assertEqual(r["unit_base"], "L")

    def test_apostrophe_brand_normalized(self):
        # "Lay's" and "Lays" must group to the same stable token "Lays".
        self.assertEqual(pf.parse_name("Lay's American Style Cream & Onion 90g")["brand"], "Lays")
        self.assertEqual(pf.parse_name("Lays Classic Salted 90g")["brand"], "Lays")

    def test_underscore_unit_separator(self):
        # "750_Ml" uses an underscore separator (common in app payloads).
        r = pf.parse_name("Dettol Handwash 750_Ml")
        self.assertEqual(r["pack_value"], 750.0)
        self.assertEqual(r["pack_unit"], "ml")

    def test_multipack_count(self):
        r = pf.parse_name("Taali Protein Puffs Pack of 2")
        self.assertEqual(r["pack_value"], 2.0)
        self.assertEqual(r["pack_unit"], "count")
        self.assertTrue(r["is_multipack"])

    def test_unparseable_input(self):
        r = pf.parse_name("")
        self.assertEqual(r["parse_status"], "unparseable")


if __name__ == "__main__":
    unittest.main(verbosity=2)
