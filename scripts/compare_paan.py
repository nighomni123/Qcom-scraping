#!/usr/bin/env python3
"""
compare_paan.py — compare OUR crawler inventory vs QuickCommerce API for
paan-shop / unconventional (non-food) items at a shared location.

Reads:
  - inventory/inventory_blinkit.db / inventory/inventory_zepto.db  (our --store-inventory output)
  - docs/qcapi_paan_sweep.json                 (API paan-term sweep)

Prints, per app, which paan-shop items OUR crawler captured vs what the API
returned, so we can see coverage gaps (esp. the Instamart wall on our side).

Usage:
  python3 scripts/compare_paan.py
"""
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_JSON = os.path.join(ROOT, "docs", "qcapi_paan_sweep.json")

# paan-shop / unconventional (non-food) keyword set
KEYWORDS = [
    "paan", "cigarette", "cig", "gutkha", "gutka", "pan masala", "tobacco",
    "bidi", "bidis", "mouth freshener", "mukhwas", "supari", "betel",
    "khaini", "zarda", "condom", "contraceptive", "lighter", "chewing",
    "areca",
]

PLATFORM_TO_APP = {"BlinkIt": "blinkit", "Zepto": "zepto", "Swiggy": "instamart"}


def our_paan_products(app):
    """Return dict name -> min price for paan-matching rows in our DB."""
    db = os.path.join(ROOT, "inventory", f"inventory_{app}.db")
    if not os.path.exists(db):
        return None
    con = sqlite3.connect(db)
    try:
        # price_obs is auto-categorized; also scan stock_obs product names
        rows = con.execute(
            "SELECT name, price, mrp FROM price_obs").fetchall()
    finally:
        con.close()
    out = {}
    for name, price, mrp in rows:
        if name is None:
            continue
        low = name.lower()
        if any(k in low for k in KEYWORDS):
            prev = out.get(name)
            p = price if price is not None else mrp
            if prev is None or (p is not None and p < prev):
                out[name] = p
    return out


def api_paan_products():
    if not os.path.exists(API_JSON):
        return {}
    data = json.load(open(API_JSON))
    # term -> app -> list of (name, offer)
    result = {}
    for term, by_plat in data.items():
        for plat, payload in by_plat.items():
            app = PLATFORM_TO_APP.get(plat)
            if not app:
                continue
            prods = payload.get("products", []) if isinstance(payload, dict) else []
            for p in prods:
                name = p.get("name")
                if not name:
                    continue
                result.setdefault(app, {}).setdefault(name, p.get("offer"))
    return result


def main():
    api = api_paan_products()
    print("=" * 78)
    print("PAAN-SHOP / UNCONVENTIONAL ITEM COVERAGE — OUR CRAWLER vs QUICKCOMMERCE API")
    print("=" * 78)
    for app in ("blinkit", "zepto", "instamart"):
        ours = our_paan_products(app)
        theirs = api.get(app, {})
        print(f"\n### {app.upper()}  "
              f"(our captured paan items: {len(ours) if ours else 0} distinct names, "
              f"API returned: {len(theirs)} distinct names)")
        if ours is None:
            print("   [our inventory DB not found — crawler may not have run / was walled]")
        our_names = set(ours or {})
        their_names = set(theirs)
        only_theirs = sorted(their_names - our_names)
        both = sorted(their_names & our_names)
        print(f"   in BOTH: {len(both)}")
        for n in both[:30]:
            print(f"      ✓ {n[:60]:60} (ours ₹{ours[n]} / api ₹{theirs[n]})")
        print(f"   ONLY via API ({len(only_theirs)}):")
        for n in only_theirs[:30]:
            print(f"      → {n[:60]:60} (api ₹{theirs[n]})")
        if our_names - their_names:
            extra = sorted(our_names - their_names)
            print(f"   ONLY in OUR crawl ({len(extra)}):")
            for n in extra[:15]:
                print(f"      * {n[:60]:60} (ours ₹{ours[n]})")


if __name__ == "__main__":
    main()
