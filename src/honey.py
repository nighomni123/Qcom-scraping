"""
honey.py — the honey-pot SKU basket.

We hardcode the TRUE price of a few high-velocity, normally-priced items. These
are our canaries: because we know what they *should* cost, any large deviation
is an immediate glitch signal — no statistics required for the first pass. This
is what lets us catch a pricing bug in the first minutes, upstream of Telegram.
"""
from __future__ import annotations


def load_honey(cfg) -> list:
    return cfg.get("anti_block", {}).get("honey_pot", []) or []


def honey_for_app(app: str, honey: list) -> list:
    return [h for h in honey if h.get("app") == app]
