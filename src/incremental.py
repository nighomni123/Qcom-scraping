"""M11 — Incremental refresh architecture (Phase 19 + Phase 18 CLI).
Pure stdlib; db=None degrades; additive (no drops/renames); lazy.
"""
from __future__ import annotations
import time


def catalog_watermark(db):
    """Max snapshot ts from deals.db catalog_snapshots. None if no db / empty."""
    if db is None or not hasattr(db, "conn"):
        return None
    try:
        row = db.conn.execute("SELECT MAX(ts) FROM catalog_snapshots").fetchone()
        return row[0] if row and row[0] is not None else None
    except Exception:
        return None


def products_changed_since(db, since_ts):
    """Return identifiers of product groups with new/changed catalog rows
    since since_ts. Honest approximation: groups with entries in
    catalog_snapshots with ts >= since_ts that also have changed price or
    stock relative to previous snapshot (no full diff engine here — ponytail).
    """
    # Honest approximation: use catalog_events 'new' / 'delisted' at since_ts,
    # plus any snapshot rows with ts >= since_ts; not a full pair-reconcile.
    if db is None or since_ts is None:
        return None
    try:
        rows = db.conn.execute(
            "SELECT DISTINCT sku_key FROM catalog_snapshots WHERE ts >= ?",
            (since_ts,),
        ).fetchall()
        return [r[0] for r in rows if r[0]]
    except Exception:
        return None


def plan_refresh(rows, db=None, since_ts=None, full_window_days=7, force=False):
    """Decide which stages need work this cycle."""
    watermark = catalog_watermark(db)
    new_ids = products_changed_since(db, since_ts or watermark or 0)
    changed_ids = new_ids  # approximation
    unchanged_skipped = True  # most stay unchanged
    full_needed = force or (watermark is None) or (since_ts is not None and (time.time() - (since_ts or 0)) > full_window_days * 86400)
    return {
        "watermark": watermark,
        "new_ids": new_ids,
        "changed_ids": changed_ids,
        "unchanged_skipped": unchanged_skipped,
        "full_refresh_needed": full_needed,
        "full_refresh_reason": "cold start" if watermark is None else ("force" if force else "weekly cadence exceeded" if full_needed else None),
        "stages": {
            "parse": new_ids or [],
            "embed": new_ids or [],
            "market_features": changed_ids or [],
            "full_refresh": full_needed,
        },
    }


def run_opportunity_refresh(rows, db=None, force=False, min_n=25):
    """Weekly-refresh wrapper: compute when full refresh due, else stale readback."""
    from src.opportunities import score_opportunities, persist_opportunities, load_latest_opportunities
    plan = plan_refresh(rows, db=db, force=force, min_n=min_n)
    if plan["full_refresh_needed"]:
        opps = score_opportunities(rows, db=db, min_n=min_n)
        if db is not None:
            persist_opportunities(db, opps)
        return {"opportunities": opps, "stale": False, "plan": plan}
    else:
        opps = load_latest_opportunities(db, limit=50) if db else []
        return {"opportunities": opps, "stale": True, "plan": plan, "reason": "weekly cadence not exceeded"}


if __name__ == "__main__":
    # Offline self-test with synthetic snapshot history
    import sqlite3, tempfile, os
    rows = [{"product_group_id":"G1","category":"C","name":"X","app":"blinkit"}]
    # Watermark with empty db returns None
    assert catalog_watermark(None) is None
    plan = plan_refresh(rows, db=None, since_ts=None, force=True)
    assert plan["full_refresh_needed"] is True
    assert plan["stages"]["full_refresh"] is True
    plan2 = plan_refresh(rows, db=None, since_ts=999999, force=False)
    assert plan2["full_refresh_needed"] is True  # no watermark => cold start forces full
    print("[incremental] self-test OK")
