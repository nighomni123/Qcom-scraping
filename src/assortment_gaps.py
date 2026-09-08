"""
assortment_gaps.py — M3 Phase 6: "assortment gap" detection.

An *assortment gap* is a product (cross-app `product_group_id`, already computed
by the M1 union layer) that is carried by AT LEAST ONE of the tracked apps in
this corridor but is MISSING on AT LEAST ONE of the others. It is a pure
COVERAGE signal derived from the union layer + `product_group_id`: no embedding
math, no demand inference.

This module is ADDITIVE only — it reads the in-memory row list returned by
`load_product_space()` and never touches the DB, the crawler, or other modules.

Honesty note baked into the design: a gap tells you "app X doesn't list this
product that app Y carries". It does NOT tell you anyone wants it on X. The
`confidence` value below therefore measures how *established* the product is on
its present apps (so the gap is a robust observation, not a capture fluke) — it
NEVER encodes demand.
"""
from __future__ import annotations

import sys
from collections import defaultdict


def _confidence(present_apps, tracked_apps, store_count):
    """Honest, coverage-only confidence in [0, 1].

    It grows with how firmly the group is established on the apps that DO carry
    it:

        present_share = len(present_apps) / len(tracked_apps)   # 1/3, 2/3, ...
        establishment = min(store_count / STORE_CAP, 1.0)        # more stores -> more real
        confidence    = 0.5*present_share + 0.5*establishment

    `establishment` rewards products seen across many darkstores on their present
    apps (less likely to be a single-store capture artifact). `present_share`
    rewards gaps that are "almost everywhere" over "only on one app". Capped at
    1.0. This number is intentionally NOT a demand estimate.
    """
    STORE_CAP = 5  # a product present on >=5 distinct stores is well-established
    total = len(tracked_apps) or 1
    present_share = len(present_apps) / total
    establishment = min(store_count / STORE_CAP, 1.0)
    return round(min(0.5 * present_share + 0.5 * establishment, 1.0), 4)


def detect_assortment_gaps(rows, tracked_apps=("blinkit", "zepto", "instamart")):
    """Return assortment-gap candidates from a product-space row list.

    `rows` is the list[dict] produced by `load_product_space()` (each row has
    `product_group_id`, `app`, `store_id`, `name`, `category`, ...). Vouchers are
    already excluded upstream; this function only does coverage analysis.

    Grouping: rows sharing a `product_group_id` form one cross-app product.
    For each group:
        present_apps = sorted(set of tracked apps with >=1 member)
        missing_apps = [a for a in tracked_apps if a not in present_apps]
    Emit a candidate ONLY when `1 <= len(present_apps) < len(tracked_apps)`
    (present on some but not all tracked apps). Groups missing from ALL tracked
    apps (e.g. only 'bigbasket') are NOT localized as gaps here and are skipped.

    Each candidate dict:
        {product_group_id, rep_name, present_apps, missing_apps,
         member_count, store_count, category, confidence}
    """
    tracked = set(a.lower() for a in tracked_apps)
    order = tuple(a.lower() for a in tracked_apps)

    by_group = defaultdict(list)
    for r in rows:
        gid = r.get("product_group_id")
        if gid is None:
            continue
        by_group[gid].append(r)

    candidates = []
    for gid, members in by_group.items():
        present = sorted({m["app"].lower() for m in members if m.get("app", "").lower() in tracked})
        if not (1 <= len(present) < len(tracked)):
            continue  # all present -> not a gap; none present -> can't localize
        missing = [a for a in order if a not in present]

        store_count = len({m.get("store_id") for m in members})
        member_count = len(members)

        # representative name = most common name in the group (tie -> first seen)
        name_counts = defaultdict(int)
        for m in members:
            name_counts[m.get("name", "")] += 1
        rep_name = max(name_counts.items(), key=lambda kv: (kv[1],))[0]

        # category = most common category in the group (fallback to "")
        cat_counts = defaultdict(int)
        for m in members:
            cat_counts[m.get("category", "")] += 1
        category = max(cat_counts.items(), key=lambda kv: (kv[1],))[0]

        candidates.append({
            "product_group_id": gid,
            "rep_name": rep_name,
            "present_apps": present,
            "missing_apps": missing,
            "member_count": member_count,
            "store_count": store_count,
            "category": category,
            "confidence": _confidence(present, order, store_count),
        })

    # stable, readable order: fewest present apps first (biggest gaps), then name
    candidates.sort(key=lambda c: (len(c["present_apps"]), c["rep_name"]))
    return candidates


if __name__ == "__main__":
    # Offline self-test (no DB, no network): rebuild the four required cases.
    def row(gid, app, store_id, name, category="Grocery"):
        return {"product_group_id": gid, "app": app, "store_id": store_id,
                "name": name, "category": category, "in_stock": 1}

    rows = [
        # G1: blinkit + zepto only -> gap missing instamart
        row("G1", "blinkit", "B1", "Amul Taaza Milk 500ml"),
        row("G1", "zepto", "Z1", "Amul Taaza Milk 500 ml"),
        # G2: all three -> NOT a gap
        row("G2", "blinkit", "B1", "Tata Salt 1kg"),
        row("G2", "zepto", "Z1", "Tata Salt 1kg"),
        row("G2", "instamart", "I1", "Tata Salt 1kg"),
        # G3: instamart only -> gap missing blinkit + zepto
        row("G3", "instamart", "I1", "Rare Local Snack"),
        # G4: only bigbasket (not tracked) -> NOT a gap
        row("G4", "bigbasket", "BB1", "Bigbasket Exclusive Tea"),
    ]

    gaps = detect_assortment_gaps(rows)
    by_gid = {g["product_group_id"]: g for g in gaps}

    ok = True
    # G1 must appear, missing instamart
    if "G1" not in by_gid or by_gid["G1"]["missing_apps"] != ["instamart"]:
        print("FAIL: G1 not detected with missing_apps=['instamart']")
        ok = False
    # G2 must NOT appear
    if "G2" in by_gid:
        print("FAIL: G2 (present on all apps) wrongly emitted as a gap")
        ok = False
    # G3 must appear, missing blinkit+zepto
    if "G3" not in by_gid or set(by_gid["G3"]["missing_apps"]) != {"blinkit", "zepto"}:
        print("FAIL: G3 not detected with missing_apps=['blinkit','zepto']")
        ok = False
    # G4 must NOT appear (none of the tracked apps carry it)
    if "G4" in by_gid:
        print("FAIL: G4 (only non-tracked app) wrongly emitted as a gap")
        ok = False
    # confidence sanity: in [0,1]
    for g in gaps:
        if not (0.0 <= g["confidence"] <= 1.0):
            print(f"FAIL: confidence out of range for {g['product_group_id']}")
            ok = False

    if not ok:
        print("[assortment_gaps] self-test FAILED")
        sys.exit(1)
    print("[assortment_gaps] self-test OK")
