"""
opportunities.py — M6 Phase 7 + 8: opportunity scoring and validation.

Merges the two gap families built so far:
  * assortment gaps (M3, src/assortment_gaps.py) — missing from N of 3 apps
  * internal/attribute gaps (M5, src/gaps.py) — sparse attribute outlier

and scores each candidate with the plan's transparent product formula:

    Opportunity Score
    = Assortment-gap strength (missing share x establishment confidence)
    × Neighborhood DPI (mean dpi of same-category groups)
    × Churn stability (0.5 if catalog_events burst = crawl noise, else 1.0)

Coverage is NOT a multiplier: categories flagged insufficient/stale coverage
are dropped by the hard guard above, so every emitted row has coverage = 1.0.

Offline note: with db=None every neutral fires at once (dpi=0.5,
coverage=1.0, churn=1.0) so offline scores are ordering-only, not calibrated:
score = 0.5 × gap_strength for all rows.

Every number cites provenance. Phase 8 validation attaches reason codes built
ONLY from existing repo signals (vouchers, search archive, density guards,
churn burst) — no new heuristics invented for this step.

Pure stdlib; reuses the other product-space modules. Offline-degradable:
`db=None` -> DPI evidence absent (neutral 0.5), churn burst unknown (neutral),
search archive check skipped.
"""
# ponytail: offline-ordering upgrade path is a has_dpi_evidence flag /
# NEUTRAL_DPI=1.0 so unevidenced rows stop being discounted; live math unchanged.
from __future__ import annotations

import json
import time
from collections import defaultdict

from .store import is_voucher_name

DPI_NORM = 5.0        # dpi (demand.py rollup) that maps to full strength
EVENT_BURST = 500     # catalog_events in 14d above this = crawl-noise suspect
NEUTRAL = 1.0
NEUTRAL_DPI = 0.5     # no DPI evidence: neutral, never a boost


def _dpi_strength(dpi):
    return round(min(dpi / DPI_NORM, 1.0), 4) if dpi is not None else NEUTRAL_DPI


def score_opportunities(rows, db=None, min_n=25):
    """Return opportunity candidates (highest score first).

    Each candidate dict:
        product_group_id, rep_name, category, gap_types, score, breakdown
        {assortment, dpi, coverage, churn}, validation_reason_codes, provenance
    """
    from .assortment_gaps import detect_assortment_gaps
    from .gaps import score_internal_gaps
    from .density import compute_density
    from .product_vectors import build_product_vectors

    vecs = build_product_vectors(rows, db=db)
    dens = compute_density(rows, min_n=min_n, db=db)
    dens_by = {d["product_group_id"]: d for d in dens}
    assoc = detect_assortment_gaps(rows)
    assoc_by = {a["product_group_id"]: a for a in assoc}
    internal = score_internal_gaps(rows, min_n=min_n, db=db)
    internal_by = {g["product_group_id"]: g for g in internal}

    # neighborhood demand proxy: per-category mean dpi of member groups
    cat_dpi = defaultdict(list)
    for gid, v in vecs.items():
        d = v["commercial_features"].get("dpi")
        if d is not None:
            cat_dpi[v["category"]].append(d)
    cat_dpi_mean = {c: sum(v) / len(v) for c, v in cat_dpi.items()}

    # crawl-noise proxy: overall catalog_events volume in the last 14 days
    burst = False
    if db is not None:
        try:
            cutoff = time.time() - 14 * 86400.0
            n = db.conn.execute(
                "SELECT COUNT(*) FROM catalog_events WHERE ts>=?", (cutoff,)
            ).fetchone()[0]
            burst = n > EVENT_BURST
        except Exception:
            pass

    tracked = ("blinkit", "zepto", "instamart")

    # Per-(category, app) coverage: does a missing app even carry this category?
    # If it carries NOTHING in the category, "missing on app X" is a crawl
    # artifact ("we haven't crawled it"), not a market gap (Phase 8 check).
    cat_app_counts = defaultdict(lambda: defaultdict(int))
    for v in vecs.values():
        for a in v["present_apps"]:
            cat_app_counts[v["category"]][a] += 1

    out = []
    for gid in set(assoc_by) | set(internal_by):
        dens_row = dens_by.get(gid) or {}
        v = vecs.get(gid) or {}
        cat = dens_row.get("category") or v.get("category") or ""
        rep = v.get("rep_name") or gid

        # Hard guard (plan's core invariant): no opportunity may come from a
        # category flagged insufficient_coverage / stale_coverage — a "gap"
        # there is far more likely "we haven't watchlisted this subcategory
        # yet" than a real market gap. Applies to BOTH gap families, since
        # assortment gaps alone carry no coverage guard upstream.
        if dens_row.get("insufficient_coverage") or dens_row.get("stale_coverage"):
            continue

        codes = []
        gap_types = []
        if gid in assoc_by:
            gap_types.append("assortment")
        if gid in internal_by:
            gap_types.append("internal")

        # --- gap strength (the plan's "assortment-gap strength" factor) ---
        strength = 0.0
        if gid in assoc_by:
            a = assoc_by[gid]
            no_cov = [m for m in a["missing_apps"]
                      if cat_app_counts[cat].get(m, 0) == 0]
            real_missing = [m for m in a["missing_apps"] if m not in no_cov]
            if not real_missing:
                # every missing app carries nothing in this category -> artifact
                codes.append("missing_app_has_no_category_coverage")
                continue
            strength = round(len(real_missing) / len(tracked) * a["confidence"], 4)
            if no_cov:
                codes.append("missing_app_has_no_category_coverage")
        if gid in internal_by:
            strength = max(strength, internal_by[gid]["internal_gap_score"])
        strength = strength or NEUTRAL

        # --- neighborhood DPI ---
        ndpi = cat_dpi_mean.get(cat)
        dpi = _dpi_strength(ndpi)

        # --- coverage confidence (always 1.0: guarded categories were dropped
        # above, so coverage enters as the guard, not a multiplier) ---
        coverage = NEUTRAL

        # --- churn stability (mild extra penalty if the group itself churned) ---
        churn = 0.5 if burst else NEUTRAL
        if v.get("commercial_features", {}).get("recent_churn_flag"):
            churn = round(churn * 0.7, 4)

        # --- score = strength × dpi × coverage × churn ---
        score = round(strength * dpi * coverage * churn, 4)

        # --- Phase 8 validation reason codes (existing repo signals only) ---
        if is_voucher_name(rep):
            codes.append("voucher_excluded")
        if burst:
            codes.append("crawl_artifact_suspect")
        if db is not None:
            try:  # does it already exist in the search archive?
                probe = " ".join(rep.lower().split())[:40]
                hit = db.conn.execute(
                    "SELECT 1 FROM search_results WHERE LOWER(name) LIKE ? LIMIT 1",
                    (f"%{probe}%",),
                )
                if hit.fetchone():
                    codes.append("exists_in_search_archive")
            except Exception:
                pass

        out.append({
            "product_group_id": gid,
            "rep_name": rep,
            "category": cat,
            "gap_types": gap_types,
            "score": score,
            "breakdown": {"gap_strength": strength, "dpi": dpi,
                          "coverage": coverage, "churn": churn},
            "validation_reason_codes": codes,
            "provenance": {
                "dpi": ("mean dpi of %d same-category group(s) from demand.dpi_table"
                        % len(cat_dpi.get(cat, []))
                        if ndpi is not None else "no DPI evidence (neutral 0.5)"),
                "coverage": "density.py guards (insufficient/stale)",
                "churn": ("catalog_events burst > %d in 14d (crawl noise)"
                          % EVENT_BURST if burst else "no catalog_events burst"),
            },
        })

    out.sort(key=lambda c: -c["score"])
    return out


# --------------------------------------------------------------------------
# M7 Phase 10 — persistence (additive table, like embed.py's `embeddings`)
# --------------------------------------------------------------------------
_OPP_SCHEMA = """
CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,
    product_group_id TEXT,
    rep_name TEXT,
    category TEXT,
    gap_types TEXT,              -- csv: assortment|internal
    score REAL,
    gap_strength REAL,
    dpi REAL,
    coverage REAL,
    churn REAL,
    validation_reason_codes TEXT,  -- csv
    provenance TEXT                -- json
);
CREATE INDEX IF NOT EXISTS idx_opp_ts ON opportunities(ts);
CREATE INDEX IF NOT EXISTS idx_opp_pgid ON opportunities(product_group_id);
"""


def persist_opportunities(db, opps):
    """Write a scored-opportunity snapshot into deals.db (additive table).

    `db` = a src.store.Store instance. Append-only: each run inserts a new
    snapshot (ts = now) so temporal/stability analysis (M9) can diff runs.
    Returns the number of rows written.
    """
    db.conn.executescript(_OPP_SCHEMA)
    now = time.time()
    try:  # keep batch ts strictly increasing so same-float persists never merge
        prev = db.conn.execute("SELECT MAX(ts) FROM opportunities").fetchone()[0]
        if prev is not None and now <= prev:
            now = prev + 0.001
    except Exception:
        pass
    rows = [
        (
            now, o["product_group_id"], o["rep_name"], o["category"],
            ",".join(o["gap_types"]), o["score"],
            o["breakdown"]["gap_strength"], o["breakdown"]["dpi"],
            o["breakdown"]["coverage"], o["breakdown"]["churn"],
            ",".join(o["validation_reason_codes"]),
            json.dumps(o["provenance"]),
        )
        for o in opps
    ]
    db.conn.executemany(
        "INSERT INTO opportunities (ts, product_group_id, rep_name, category,"
        " gap_types, score, gap_strength, dpi, coverage, churn,"
        " validation_reason_codes, provenance) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    db.conn.commit()
    return len(rows)


def load_latest_opportunities(db, limit=50):
    """Latest persisted opportunity snapshot (highest score first)."""
    db.conn.executescript(_OPP_SCHEMA)
    # Snapshot selection is id-anchored, never float equality: the latest
    # batch's max id gives THE snapshot ts; rows are that ts AND id <= max id.
    mx = db.conn.execute("SELECT MAX(id) FROM opportunities").fetchone()[0]
    if mx is None:
        return []
    snap_ts = db.conn.execute(
        "SELECT ts FROM opportunities WHERE id=?", (mx,)).fetchone()[0]
    rows = db.conn.execute(
        "SELECT ts, product_group_id, rep_name, category, gap_types, score,"
        " gap_strength, dpi, coverage, churn, validation_reason_codes, provenance"
        " FROM opportunities WHERE ts = ? AND id <= ?"
        " ORDER BY score DESC LIMIT ?",
        (snap_ts, mx, limit,),
    ).fetchall()
    return [
        {
            "ts": r[0], "product_group_id": r[1], "rep_name": r[2],
            "category": r[3], "gap_types": r[4].split(",") if r[4] else [],
            "score": r[5], "breakdown": {
                "gap_strength": r[6], "dpi": r[7], "coverage": r[8], "churn": r[9],
            },
            "validation_reason_codes": r[10].split(",") if r[10] else [],
            "provenance": json.loads(r[11]) if r[11] else {},
        }
        for r in rows
    ]


if __name__ == "__main__":
    # Offline self-test (no DB): an assortment+internal gap in a well-covered
    # category scores > 0; under-covered categories never leak.
    def mk(gid, cat, name, up, pv, brand, app="blinkit"):
        return dict(product_group_id=gid, category=cat, name=name, app=app,
                    store_id=app + "1", sku_key=app + "-" + name,
                    unit_price=up, pack_value=pv, brand=brand, in_stock=1)

    rows = []
    for i in range(30):
        rows.append(mk(f"S{i}", "CatA", f"Spice {i}", 10.0 + i * 0.1,
                       100.0 + i, "Generic"))
    # instamart present in CatA -> coverage exists for the "missing" app check,
    # so GA's assortment gap is a REAL gap, not a crawl artifact
    rows.append(mk("SX", "CatA", "Instamart Cover Spice", 15.0, 150.0,
                   "Generic", "instamart"))
    # GA: two apps (assortment gap missing instamart) + attribute outlier
    rows.append(mk("GA", "CatA", "Rare Exotic Spice", 900.0, 50.0, "Exotic", "blinkit"))
    rows.append(mk("GA", "CatA", "Rare Exotic Spice", 905.0, 50.0, "Exotic", "zepto"))
    # under-covered category -> guarded
    rows.append(mk("R1", "Rare", "Odd Item 1", 20.0, 200.0, "X"))
    rows.append(mk("R2", "Rare", "Odd Item 2", 25.0, 220.0, "X"))

    opps = score_opportunities(rows, min_n=25)
    ids = {o["product_group_id"] for o in opps}
    assert "GA" in ids, "GA (assortment+internal) must surface"
    assert "R1" not in ids and "R2" not in ids, "guarded category leaked"
    ga = next(o for o in opps if o["product_group_id"] == "GA")
    assert sorted(ga["gap_types"]) == ["assortment", "internal"], ga["gap_types"]
    assert ga["score"] > 0, "score must be positive"
    # breakdown must be a visible product of the components
    assert ga["score"] == round(
        ga["breakdown"]["gap_strength"] * ga["breakdown"]["dpi"] *
        ga["breakdown"]["coverage"] * ga["breakdown"]["churn"], 4), ga
    assert isinstance(ga["validation_reason_codes"], list)

    # --- persistence round-trip on a scratch DB (M7) ---
    import sqlite3, tempfile, os as _os
    class _Conn:
        def __init__(self, conn):
            self.conn = conn
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    try:
        scratch = _Conn(sqlite3.connect(tmp.name))
        n = persist_opportunities(scratch, opps)
        assert n == len(opps), f"persisted {n} != {len(opps)}"
        back = load_latest_opportunities(scratch, limit=50)
        assert len(back) == len(opps), "round-trip count mismatch"
        back_by = {o["product_group_id"]: o for o in back}
        assert back_by["GA"]["score"] == ga["score"], "score not preserved"
        assert back_by["GA"]["gap_types"] == ga["gap_types"], "gap_types lost"
        assert isinstance(back_by["GA"]["provenance"], dict), "provenance lost"
        # second snapshot appends, latest wins
        persist_opportunities(scratch, opps[:3])
        assert len(load_latest_opportunities(scratch, limit=50)) == 3
        scratch.conn.close()
    finally:
        _os.unlink(tmp.name)

    print("[opportunities] self-test OK")