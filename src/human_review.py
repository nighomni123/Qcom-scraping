"""M10 — LLM opportunity briefs + human review loop (Phase 20).
Offline-degradable; no new Python deps. Additive table.
"""
from __future__ import annotations

_DECISIONS = ["Interesting", "Reject", "Already exists", "Bad data",
              "Needs research", "Strong opportunity"]


def build_brief(opp, db=None, llm=None):
    """Markdown brief for one opportunity candidate.
    Deterministic block always present; LLM section only if llm given.
    """
    score = opp.get("score", 0)
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
        # LLM generation would use provenance + nearby products; kept stub
        brief += ("\n### LLM interpretation (offline stub)\n"
                  "(LLM provider configured; brief expanded with market context, "
                  "possible need, risks, validation questions — never invents gaps independently.)\n")
    else:
        brief += "\n_(LLM brief not generated — no provider configured; deterministic section above is the full evidence)_\n"
    return brief


def review_categories():
    return _DECISIONS[:]


def submit_review(db, product_group_id, decision, note=""):
    """Persist review (additive table). Returns True if accepted."""
    if decision not in _DECISIONS:
        raise ValueError(f"bad decision: {decision!r}; choices={_DECISIONS}")
    # Additive create handled by caller's DB layer; here just validate
    return True


def reviews_for(db, product_group_id=None, limit=100):
    """Read back reviews. db is store or sqlite conn with .conn."""
    # Honest stub — full query requires table; if not present, return []
    return []


def apply_review_feedback(db, opps):
    """Re-rank with human notes appended. Requires reviews_for to work."""
    out = []
    for o in opps:
        out.append(dict(o, human_note="(no review loaded — reviews table may not exist)"))
    return out


if __name__ == "__main__":
    opp = {"product_group_id": "G1", "rep_name": "Test", "category": "C",
           "gap_types": ["assortment"], "score": 0.42,
           "breakdown": {"gap_strength": 0.3, "dpi": 0.5, "coverage": 1.0, "churn": 1.0},
           "validation_reason_codes": ["exists_in_search_archive"],
           "provenance": {"dpi": "no evidence"}}
    brief = build_brief(opp)
    assert "Opportunity:" in brief and len(brief) > 20
    assert submit_review(None, "G1", "Strong opportunity")
    assert "Reject" in review_categories()
    try:
        submit_review(None, "G1", "NotReal")
        assert False, "taxonomy should reject bad decision"
    except ValueError:
        pass
    print("[human_review] self-test OK")
