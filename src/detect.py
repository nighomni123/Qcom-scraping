"""
detect.py — glitch / anomaly scoring.

Two layers:
  1. Honey-pot: deviation vs hardcoded TRUE price (instant, high confidence).
  2. Statistical: z-score of current price vs the per-store rolling baseline.
     Because QC prices are local, we baseline per (store, sku), not globally.

Returns (is_glitch: bool, score: float, reason: str).
"""
from __future__ import annotations

import statistics


def _zscore(value, values):
    if len(values) < 3:
        return 0.0
    m = statistics.mean(values)
    sd = statistics.pstdev(values)
    if sd == 0:
        return 0.0
    return (m - value) / sd  # positive => cheaper than norm


def _honey_match(honey, app, sku_key, name):
    """Return (true_price) if this product matches a canary, else None.
    Matches on token overlap so live catalog names ('Amul Gold Milk 1L')
    still hit the 'amul milk' canary."""
    n = f"{sku_key} {name or ''}".lower()
    for h in honey:
        if h.get("app") != app:
            continue
        q = (h.get("query") or "").lower().split()
        if q and all(tok in n for tok in q):
            return h.get("true_price")
    return None


def evaluate(app, store_id, sku_key, price, mrp, cfg, store, honey=None, name=None):
    dcfg = cfg.get("detect", {})
    honey_dev = dcfg.get("honey_deviation_pct", 25)
    z_thr = dcfg.get("stat_zscore", 2.5)
    min_off = dcfg.get("min_margin_off_mrp", 40)
    max_price = dcfg.get("max_price", 5000)

    if price is None or price <= 0:
        return False, 0.0, ""
    if price > max_price:
        return False, 0.0, "price over cap"

    # Layer 1: honey-pot deviation (token-overlap match on name/sku)
    if honey:
        true_p = _honey_match(honey, app, sku_key, name)
        if true_p:
            dev = (true_p - price) / true_p * 100
            if dev >= honey_dev:
                return True, round(dev, 1), f"honey -{dev:.0f}% vs true ₹{true_p}"
            # canary priced ABOVE its known-good price is also interesting
            if dev <= -30:
                return True, round(-dev, 1), f"honey +{-dev:.0f}% above true ₹{true_p}"

    # MRP margin gate
    if mrp and mrp > 0:
        off = (mrp - price) / mrp * 100
        if off < min_off:
            # cheap but not a real deal vs MRP; still allow stat outliers
            pass

    # Layer 2: statistical outlier vs store baseline
    hist = store.window(app, store_id, sku_key)
    if hist:
        z = _zscore(price, hist)
        if z >= z_thr:
            return True, round(z, 2), f"z={z:.1f} below store norm"

    # First-seen honey / MRP deal w/ big margin
    if mrp and mrp > 0:
        off = (mrp - price) / mrp * 100
        if off >= min_off and len(hist) >= 1:
            # only flag if it's clearly below its own recent price too
            if not hist or price <= min(hist) * 0.8:
                return True, round(off, 1), f"-{off:.0f}% off MRP"

    return False, 0.0, ""
