"""M10 — LLM opportunity briefs + human review loop (Phase 20).
Offline-degradable; no new Python deps. Additive table opportunity_reviews.
"""
from __future__ import annotations

import time

_DECISIONS = ["Interesting", "Reject", "Already exists", "Bad data",
              "Needs research", "Strong opportunity"]

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS opportunity_reviews ("
    "review_id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "product_group_id TEXT NOT NULL, decision TEXT NOT NULL, "
    "note TEXT DEFAULT '', ts REAL NOT NULL)"
)


def build_brief(opp, db=None, llm=None):
    """Markdown brief for one opportunity candidate.
    Deterministic block always present; LLM section only if llm given.
    """
    # ponytail: ceiling=deterministic evidence only, upgrade=LLM call grounded on candidate+neighbors, never inventing gaps
    score = float(opp.get("score") or 0)
    breakdown = opp.get("breakdown") or {}
    provenance = opp.get("provenance") or {}
    codes = opp.get("validation_reason_codes") or []
    brief = (
        f"## Opportunity: {opp.get('rep_name', '—')} (group {opp.get('product_group_id')})\n\n"
        f"- **Category:** {opp.get('category', '—')}\n"
        f"- **Score:** {score:.4f}\n"
        f"- **Gap types:** {', '.join(opp.get('gap_types') or [])}\n"
        f"- **Breakdown:** gap_strength={breakdown.get('gap_strength')}, dpi={breakdown.get('dpi')}, "
        f"coverage={breakdown.get('coverage')}, churn={breakdown.get('churn')}\n"
        f"- **Validation codes:** {', '.join(codes)}\n"
        f"- **Provenance:** {str(provenance)[:200]}\n"
    )
    if llm is not None:
        brief += ("\n### LLM interpretation (not yet wired — stub)\n"
                  "(LLM provider configured; brief expanded with market context, "
                  "possible need, risks, validation questions — never invents gaps independently.)\n")
    else:
        brief += "\n_(no LLM interpretation — deterministic evidence above)_\n"
    return brief


def review_categories():
    return _DECISIONS[:]


def _conn(db):
    return db.conn if hasattr(db, "conn") else db


def submit_review(db, product_group_id, decision, note=""):
    """Persist review into additive opportunity_reviews table. Returns True."""
    if decision not in _DECISIONS:
        raise ValueError(f"bad decision: {decision!r}; choices={_DECISIONS}")
    if db is None:
        raise ValueError("db is required — reviews persist nowhere silently")
    conn = _conn(db)
    conn.execute(_SCHEMA)
    conn.execute(
        "INSERT INTO opportunity_reviews (product_group_id, decision, note, ts)"
        " VALUES (?,?,?,?)",
        (product_group_id, decision, note or "", time.time()),
    )
    try:
        conn.commit()
    except Exception:
        pass
    return True


def reviews_for(db, product_group_id=None, limit=100):
    """Read back reviews (newest first). db is store or sqlite conn."""
    if db is None:
        return []
    try:
        conn = _conn(db)
        conn.execute(_SCHEMA)
        if product_group_id is not None:
            rows = conn.execute(
                "SELECT product_group_id, decision, note, ts FROM opportunity_reviews"
                " WHERE product_group_id = ? ORDER BY ts DESC LIMIT ?",
                (product_group_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT product_group_id, decision, note, ts FROM opportunity_reviews"
                " ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [{"product_group_id": r[0], "decision": r[1], "note": r[2], "ts": r[3]} for r in rows]
    except Exception:
        return []


def apply_review_feedback(db, opps):
    """Annotate opps with their latest review; omit key when no review exists."""
    revs = reviews_for(db) if db is not None else []
    latest = {}
    for r in revs:
        latest.setdefault(r["product_group_id"], r)
    out = []
    for o in opps:
        d = dict(o)
        if o.get("product_group_id") in latest:
            r = latest[o["product_group_id"]]
            d["human_note"] = f"{r['decision']}: {r['note']}".strip(": ")
            d["human_decision"] = r["decision"]
        out.append(d)
    return out


if __name__ == "__main__":
    import sqlite3
    opp = {"product_group_id": "G1", "rep_name": "Test", "category": "C",
           "gap_types": ["assortment"], "score": None,
           "breakdown": {"gap_strength": 0.3, "dpi": 0.5, "coverage": 1.0, "churn": 1.0},
           "validation_reason_codes": ["exists_in_search_archive"],
           "provenance": {"dpi": "no evidence"}}
    brief = build_brief(opp)
    assert "Opportunity:" in brief and "(no LLM interpretation" in brief
    assert len(review_categories()) == 6
    db = sqlite3.connect(":memory:")
    assert submit_review(db, "G1", "Strong opportunity", note="looks real")
    assert reviews_for(db, "G1")[0]["decision"] == "Strong opportunity"
    assert apply_review_feedback(db, [opp])[0]["human_decision"] == "Strong opportunity"
    assert "human_note" not in apply_review_feedback(db, [{"product_group_id": "Gx"}])[0]
    try:
        submit_review(None, "G1", "Strong opportunity")
        assert False
    except ValueError:
        pass
    try:
        submit_review(db, "G1", "NotReal")
        assert False, "taxonomy should reject bad decision"
    except ValueError:
        pass
    print("[human_review] self-test OK")
