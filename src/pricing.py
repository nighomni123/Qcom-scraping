"""
pricing.py — effective-price calculator.

Raw listed price is never the whole story on Indian e-commerce:
  * quick-commerce charges delivery below a free threshold
  * bank/card/coupon codes knock real money off at checkout

This module combines both into one comparable number per platform, driven by a
user-editable knowledge base (codes.yaml). Offers are applied automatically
when the cart qualifies; each result carries its note so you can verify at
checkout.
"""
from __future__ import annotations

import os

try:
    import yaml
    _HAVE_YAML = True
except ModuleNotFoundError:
    from . import miniyaml
    _HAVE_YAML = False


DEFAULT_FEES = {
    "blinkit":   {"free_above": 100, "fee": 25},
    "zepto":     {"free_above": 190, "fee": 25},
    "instamart": {"free_above": 99,  "fee": 30},
    "amazon":    {"free_above": 499, "fee": 40},
    "flipkart":  {"free_above": 499, "fee": 40},
}


def load_pricing_kb(path="codes.yaml"):
    """Load delivery-fee model + offers. Falls back to sane defaults."""
    fees, offers = dict(DEFAULT_FEES), []
    if os.path.exists(path):
        try:
            data = (yaml.safe_load(open(path)) or {}) if _HAVE_YAML else miniyaml.load(path)
            fees.update(data.get("delivery_fees", {}))
            offers = data.get("offers", []) or []
        except Exception:
            pass
    return fees, offers


def best_offer(platform, price, offers):
    """Best applicable offer for this platform at this price.
    Returns (discount_amount, code_label, note) or (0, None, None)."""
    best = (0.0, None, None)
    for o in offers:
        if o.get("platform") != platform:
            continue
        min_order = float(o.get("min_order") or 0)
        if price < min_order:
            continue
        if o.get("type") == "flat":
            disc = float(o.get("value") or 0)
        else:  # pct
            disc = price * float(o.get("value") or 0) / 100.0
            cap = o.get("max_cap")
            if cap:
                disc = min(disc, float(cap))
        disc = min(disc, price)
        if disc > best[0]:
            best = (disc, o.get("code"), o.get("note"))
    return best


def effective_price(platform, price, fees=None, offers=None):
    """Returns dict with the comparable bottom line."""
    fees = fees or DEFAULT_FEES
    offers = offers or []
    f = fees.get(platform, {"free_above": 0, "fee": 0})
    discount, code, note = best_offer(platform, price, offers)
    net = max(price - discount, 0.0)
    delivery = 0.0 if (f.get("free_above", 0) and net >= f["free_above"]) else f.get("fee", 0.0)
    return {
        "listed": round(price, 2),
        "discount": round(discount, 2),
        "code": code,
        "note": note,
        "net": round(net, 2),
        "delivery": round(delivery, 2),
        "effective": round(net + delivery, 2),
    }
