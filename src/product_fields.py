"""
product_fields.py — grocery-shaped name parser for the Product-Space
Intelligence engine (M1, Phase 1).

Extracts brand / pack size / unit / variant / is_multipack from free-text
quick-commerce product names via regex + small unit tables. Pure stdlib, no new
dependency. Best-effort by design: Indian FMCG names are noisy, so ~70-85% clean
parse on the first pass is expected and fine — what matters is that misses are
*flagged*, never silently turned into a wrong value.

Key invariant (from the M1 review): a name with no detectable pack yields
`unit_price=None` (status `'no_pack'`), NEVER a zero. "We haven't observed the
pack" must not masquerade as "₹0" — that would corrupt every unit-price axis
the gap engine later builds.

Brand extraction = a curated multi-word prefix list (the ambiguous cases where
the first word is generic, e.g. "Metro Living", "Mother Dairy", "India Gate")
plus a heuristic fallback (first capitalized token). Misses are logged, not
dropped, so the dictionary can grow from real data.
"""
from __future__ import annotations

import logging
import re
import sys

log = logging.getLogger("product_fields")

# --------------------------------------------------------------------------
# unit normalization: canonical units are 'ml' and 'g'; counts are 'count'
# --------------------------------------------------------------------------
_VOL_RE = re.compile(r"(\d+(?:\.\d+)?)[\s_]*(ml|millilitre|milliliter|l|ltr|litre|liter)\b", re.I)
_WT_RE = re.compile(r"(\d+(?:\.\d+)?)[\s_]*(gm|grm|gram|g|kg|kgs|kilogram)\b", re.I)
_PACKOF_RE = re.compile(r"pack\s*of\s*(\d+)", re.I)
_COUNT_RE = re.compile(r"\b(\d+)\s*(?:pcs|pieces|count|pulls|pages|rolls?|sachets?|units?|tabs?|tablets?)\b", re.I)
# multipack like "2 x 500ml" / "3 x 1kg". RHS must be numeric (or numeric+unit)
# so flavour separators like "Mango x Chilli" are NOT mistaken for a multipack.
_MULTI_RE = re.compile(r"\b(\d+)\s*[x×]\s*(\d+(?:\.\d+)?\s*(?:ml|l|ltr|g|gm|grm|kg)?)\b", re.I)

_MULTIWORD_BRANDS = [
    "mother dairy", "india gate", "baker's loaf", "bakers loaf", "metro living",
    "organic tattva", "organic tattva", "love & cheesecake", "yoga bar",
    "drawguud", "prettykrafts", "homestrap", "wingreens farms", "wingreens",
    "mr. white", "mamaearth", "4700bc", "del monte", "boAt", "boat",
    "maharshil", "maharshi ayurveda", "natureland organics", "patanjali",
    "amul", "nestle", "nestlé", "britannia", "parle", "itc", "dabur", "tata",
    "hul", "hindustan unilever", "bikaji", "haldiram", "haldirams", "mondelez",
    "cadbury", "procter & gamble", "p&g", "gillette", "whisper", "dettol",
    "savlon", "dove", "lifebuoy", "wheel", "surf excel", "surf", "rin",
    "ariel", "vim", "lizol", "domex", "fortune", "saffola", "figaro",
    "leonardo", "borges", "milky mist", "provilac", "bombay", "dukes",
    "bingo", "lays", "kurkure", "maggi", "knorr", "quaker", "kellogg",
    "kelloggs", "fae beauty", "taali", "namhya", "zoff", "bedekar",
    "balaji", "cookie man", "origami", "pintola", "xclamation", "zeyu",
    "uttam", "cetaphil", "dot & key", "dermatouch", "minimalist", "snackible",
    "boldfit", "callidus", "manforce", "skore", "del monte", "doppio",
    "korebi", "mom ", "jet klin",
    # common beverage brands (often 1-2 capitalized words)
    "coca cola", "pepsi", "thums up", "fanta", "sprite", "mountain dew",
    "red bull", "monster", "tropicana", "bisleri", "kinley", "aquafina",
    "maaza", "slice", "limca", "appy fizz", "real", "paper boat",
    "nandini",
]
# sort longest-first so "baker's loaf" wins over "loaf"
_MULTIWORD_BRANDS.sort(key=len, reverse=True)

_VARIANTS = [
    "full cream", "double toned", "toned", "skimmed", "skim", "low fat",
    "fat free", "zero", "classic", "premium", "sensitive", "smooth", "creamy",
    "crunchy", "masala", "plain", "roasted", "fried", "organic", "instant",
    "whole", "original", "natural", "herbal", "ayurvedic", "sugar free",
    "diet", "diabetic", "pro", "mini", "large", "small", "extra", "mint",
    "lime", "lemon", "orange", "mango", "chocolate", "vanilla", "strawberry",
    "butter", "salted", "unsalted", "saffron", "sandalwood", "oily", "dry",
]
_VARIANTS.sort(key=len, reverse=True)

# words that, when leading, are NOT the brand (generic descriptors / pack words)
_DESCRIPTOR_STOP = {
    "fresh", "new", "organic", "premium", "best", "super", "soft", "hard",
    "the", "a", "an", "and", "with", "pack", "combo",
}


def _norm_unit(raw):
    """Return (canonical_unit, multiplier) mapping the raw unit token to ml/g."""
    r = raw.lower()
    if r in ("l", "ltr", "litre", "liter"):
        return "ml", 1000.0
    if r in ("ml", "millilitre", "milliliter"):
        return "ml", 1.0
    if r in ("kg", "kgs", "kilogram"):
        return "g", 1000.0
    if r in ("g", "gm", "grm", "gram"):
        return "g", 1.0
    return raw.lower(), 1.0


def parse_pack(name):
    """Return (pack_value, pack_unit, is_multipack).

    pack_unit is one of 'ml' | 'g' | 'count' | None.
    pack_value is normalized (L->ml, kg->g) or None when nothing detectable.
    """
    if not name:
        return None, None, False
    low = name.lower()

    # multipack: "2 x 500ml", "3 x 1kg", "2 x 6" (count)
    m = _MULTI_RE.search(name)
    if m:
        n = int(m.group(1))
        rest = m.group(2).strip()
        vm = re.match(r"(\d+(?:\.\d+)?)\s*(ml|l|ltr|g|gm|grm|kg)?", rest, re.I)
        if vm and vm.group(2):
            u, mult = _norm_unit(vm.group(2))
            return float(vm.group(1)) * mult, u, True
        # "2 x" with no trailing unit -> count multipack
        return float(n), "count", True

    m = _VOL_RE.search(name)
    if m:
        u, mult = _norm_unit(m.group(2))
        return float(m.group(1)) * mult, u, False

    m = _WT_RE.search(name)
    if m:
        u, mult = _norm_unit(m.group(2))
        return float(m.group(1)) * mult, u, False

    m = _PACKOF_RE.search(name)
    if m:
        return float(m.group(1)), "count", True

    m = _COUNT_RE.search(name)
    if m:
        return float(m.group(1)), "count", False

    return None, None, False


def _brand_span(name, needle):
    """Return the original-case span of `needle` (a lowercase, apostrophe-free
    substring) within `name`, tolerating apostrophes in `name` so 'Baker's Loaf'
    maps cleanly back to its source span."""
    norm = name.lower().replace("'", "")
    start = norm.find(needle)
    if start < 0:
        return None
    end = start + len(needle)
    orig, ni = [], 0
    for ch in name:
        if ni == end:
            break
        if ch == "'":
            continue
        if ni >= start:
            orig.append(ch)
        ni += 1
    return "".join(orig)


def parse_brand(name):
    """Return brand string or None. Curated multi-word prefixes first, then
    first-capitalized-token heuristic. Apostrophes are normalized so "Lay's"
    and "Lays" group to the same stable brand token."""
    if not name:
        return None
    low = name.lower().replace("'", "").strip()
    # multi-word brand prefixes (longest first)
    for b in _MULTIWORD_BRANDS:
        bn = b.replace("'", "")
        if low.startswith(bn) or f" {bn} " in f" {low} ":
            return _brand_span(name, bn)
    # heuristic: first token if it looks like a brand (Titlecase / ALLCAPS)
    first = name.strip().split()[0] if name.strip() else ""
    if not first:
        return None
    if first[0].isupper() and first.lower().replace("'", "") not in _DESCRIPTOR_STOP:
        # strip a trailing period (e.g. "boAt.") / comma / apostrophe
        return first.rstrip(".,").replace("'", "")
    return None


def parse_variant(name):
    """Best-effort variant/flavour token(s); may be None."""
    if not name:
        return None
    low = name.lower()
    hits = [v for v in _VARIANTS if v in low]
    return " ".join(hits) if hits else None


def unit_price(price, pack_value, pack_unit):
    """Price per canonical base unit: ₹/L for ml, ₹/kg for g, ₹/each for count.

    Returns None when price or pack_value is missing — never a fabricated 0.
    """
    if price is None or pack_value is None or pack_value <= 0:
        return None
    try:
        p = float(price)
    except (TypeError, ValueError):
        return None
    if pack_unit == "ml":
        return round(p / (pack_value / 1000.0), 2)
    if pack_unit == "g":
        return round(p / (pack_value / 1000.0), 2)
    if pack_unit == "count":
        return round(p / pack_value, 2)
    return None


def parse_name(name, price=None, category=None):
    """Single entry point. Returns a dict with parsed grocery fields.

    parse_status: 'ok' (pack detected), 'no_pack' (no pack detectable,
    unit_price None), 'unparseable' (empty/garbage input).
    """
    if not name or not str(name).strip():
        return {
            "brand": None, "pack_value": None, "pack_unit": None,
            "is_multipack": False, "variant": None, "unit_base": None,
            "unit_price": None, "parse_status": "unparseable",
        }
    brand = parse_brand(name)
    pack_value, pack_unit, is_mp = parse_pack(name)
    variant = parse_variant(name)
    if pack_unit == "ml":
        unit_base = "L"
    elif pack_unit == "g":
        unit_base = "kg"
    elif pack_unit == "count":
        unit_base = "each"
    else:
        unit_base = None
    up = unit_price(price, pack_value, pack_unit)
    status = "ok" if pack_value is not None else "no_pack"
    return {
        "brand": brand,
        "pack_value": pack_value,
        "pack_unit": pack_unit,
        "is_multipack": is_mp,
        "variant": variant,
        "unit_base": unit_base,
        "unit_price": up,
        "parse_status": status,
    }


# --------------------------------------------------------------------------
# curated self-test cases: name -> expected (brand, pack_value, pack_unit, is_multipack)
# pack_value is in canonical units (ml/g/count) after normalization.
# --------------------------------------------------------------------------
SELFTEST_CASES = [
    ("Amul Taaza Toned Milk 500ml", ("Amul", 500.0, "ml", False)),
    ("Amul Taaza Milk 500 ml", ("Amul", 500.0, "ml", False)),
    ("Amul Taaza Toned Milk 1L", ("Amul", 1000.0, "ml", False)),
    ("Britannia Gold Cake 1kg", ("Britannia", 1000.0, "g", False)),
    ("Nestle Everyday Dairy Whitener 200 g", ("Nestle", 200.0, "g", False)),
    ("Lay's American Style Cream & Onion 90g", ("Lays", 90.0, "g", False)),
    ("Tata Salt 1kg", ("Tata", 1000.0, "g", False)),
    ("Coca Cola 2L Pet Bottle", ("Coca Cola", 2000.0, "ml", False)),
    ("Dove Sandalwood Bar 75g", ("Dove", 75.0, "g", False)),
    ("Metro Living Plastic Pedal Dustbin 7L", ("Metro Living", 7000.0, "ml", False)),
    ("India Gate Everyday Basmati Rice 5kg", ("India Gate", 5000.0, "g", False)),
    ("Taali Protein Puffs Pack of 2", ("Taali", 2.0, "count", True)),
    ("Boldfit Tennis Cricket Ball Pack of 3", ("Boldfit", 3.0, "count", True)),
    ("Misfits Prebiotic Soda 250ml", ("Misfits", 250.0, "ml", False)),
    ("Bikaji Gulab Jamun 500 g", ("Bikaji", 500.0, "g", False)),
    ("Yoga Bar Power Up 20g Protein Bar", ("Yoga Bar", 20.0, "g", False)),
    ("Mother Dairy Full Cream Milk 500ml", ("Mother Dairy", 500.0, "ml", False)),
    ("Amul Taaza Toned Milk 2 x 500ml", ("Amul", 500.0, "ml", True)),
    ("Baker's Loaf Multigrain Bread", ("Bakers Loaf", None, None, False)),  # no pack
    ("Fresh Banana", (None, None, None, False)),  # no brand, no pack
]


def _run_selftest():
    fails = 0
    for name, expected in SELFTEST_CASES:
        got = parse_name(name)
        exp_brand, exp_val, exp_unit, exp_mp = expected
        ok = (got["brand"] == exp_brand and got["pack_value"] == exp_val
              and got["pack_unit"] == exp_unit and got["is_multipack"] == exp_mp)
        if not ok:
            fails += 1
            print(f"  FAIL: {name!r}\n        got={got['brand']!r},{got['pack_value']!r},"
                  f"{got['pack_unit']!r},{got['is_multipack']!r}\n        exp="
                  f"{exp_brand!r},{exp_val!r},{exp_unit!r},{exp_mp!r}")
    if fails:
        print(f"[product_fields] self-test FAILED: {fails}/{len(SELFTEST_CASES)} cases")
        return False
    print(f"[product_fields] self-test OK: {len(SELFTEST_CASES)}/{len(SELFTEST_CASES)} cases")
    return True


def sample_report(db_path="deals.db", n=200, out_csv=None):
    """Sample N distinct names from deals.db, parse them, and report status
    tallies. Writes a CSV (name, brand, pack_value, pack_unit, is_multipack,
    variant, unit_price, parse_status) for human accuracy review. Returns the
    tally dict. (This is the 80%-accuracy measurement hook — a human reviews
    the CSV; the parser never auto-declares accuracy.)"""
    import csv
    import os
    import sqlite3

    if not os.path.exists(db_path):
        print(f"[product_fields] db not found: {db_path}")
        return {}
    con = sqlite3.connect(f"file:{os.path.abspath(db_path)}?mode=ro", uri=True)
    rows = [r[0] for r in con.execute(
        "SELECT DISTINCT name FROM catalog_snapshots WHERE name IS NOT NULL "
        "ORDER BY RANDOM() LIMIT ?", (n,))]
    con.close()
    tally = {"ok": 0, "no_pack": 0, "unparseable": 0}
    out = out_csv or "exports/product_fields_sample.csv"
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["name", "brand", "pack_value", "pack_unit", "is_multipack",
                    "variant", "unit_price", "parse_status"])
        for name in rows:
            p = parse_name(name)
            tally[p["parse_status"]] = tally.get(p["parse_status"], 0) + 1
            w.writerow([name, p["brand"], p["pack_value"], p["pack_unit"],
                        p["is_multipack"], p["variant"], p["unit_price"],
                        p["parse_status"]])
    print(f"[product_fields] sampled {len(rows)} names -> {out}")
    print(f"  status: {tally}")
    print("  (review the CSV for brand/pack accuracy against the >=80% gate)")
    return tally


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    if "--report" in sys.argv:
        i = sys.argv.index("--report")
        rest = sys.argv[i + 1:]
        sample_n = 200
        db = "deals.db"
        for j, a in enumerate(rest):
            if a == "--sample" and j + 1 < len(rest):
                sample_n = int(rest[j + 1])
            elif a == "--db" and j + 1 < len(rest):
                db = rest[j + 1]
        sample_report(db, sample_n)
    else:
        ok = _run_selftest()
        sys.exit(0 if ok else 1)
