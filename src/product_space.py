"""
product_space.py — M1 Phase 2: normalized product-space union with
entity-resolution cross-app identity and full provenance.

The scraper captures truth; this layer INTERPRETS it. It reads (read-only):

  * inventory_<app>.db . inventory_catalog  (rich per-store capture: url, raw_json,
    collections = SOURCE/APP taxonomy, category = INTERNAL taxonomy)
  * deals.db ............ catalog_snapshots (cross-store catalog; coverage for
    stores not yet re-captured via the rich path)

and returns a stdlib list[dict] (no pandas). Vouchers are excluded via
is_voucher_name. Every returned record carries app/store_id/sku_key so it is
traceable back to its inventory_catalog row -> raw_json (see provenance test).

Cross-app identity is ENTITY RESOLUTION, not a bare threshold:
  1. exact canonical key (brand + normalized name + pack)  -> same group
  2. matcher (search.match_score >= 0.5) as CANDIDATE GENERATOR within brand
  3. PACK-SIZE / VARIANT VETO  -> a 500ml and 1L of the same line are DIFFERENT
     groups despite high text similarity (pack/variant are vetoes, not features)
  4. semantic embedding cosine as a CONFIRMING fallback only (never overrides a
     pack/variant veto). Degrades gracefully when no embeddings are attached.
"""
from __future__ import annotations

import array
import logging
import os
import sqlite3

from .categories import categorize
from .store import is_voucher_name
from .product_fields import (
    parse_name, _VOL_RE, _WT_RE, _PACKOF_RE, _COUNT_RE, _MULTI_RE, _VARIANTS,
)
from .search import _tokens

log = logging.getLogger("product_space")

APPS = ["blinkit", "zepto", "instamart"]
CANDIDATE_THRESHOLD = 0.5      # match_score for candidate generation
SEM_GROUP_THRESH = 0.82        # cosine for semantic confirmation (M2 vectors)
_EMBED_MODELS = [
    "nvidia/llama-nemotron-embed-vl-1b-v2",
    "embeddinggemma",
    "gemini-embedding-001",
]


def _root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _table_exists(con, name):
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _blob_to_vec(blob):
    a = array.array("f")
    a.frombytes(blob)
    return list(a)


def _dims_for(con, model):
    r = con.execute(
        "SELECT dims FROM embeddings WHERE model=? LIMIT 1", (model,)
    ).fetchone()
    return r[0] if r else None


def _norm_product_name(name):
    """Normalize a product name to its pack/variant-stripped core, for the exact
    canonical key. Lowercased, punctuation-collapsed, whitespace-trimmed."""
    import re
    n = (name or "").lower()
    for rx in (_MULTI_RE, _VOL_RE, _WT_RE, _PACKOF_RE, _COUNT_RE):
        n = rx.sub(" ", n)
    # measured: 63 str.replace passes BEAT a 63-branch alternation regex here
    # (unlike parse_brand, every needle is replaced — no early exit to win)
    for v in _VARIANTS:
        n = n.replace(v, " ")
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def _extract_rating(raw_json):
    """Parse numeric 'rating' from raw JSON payload (Blinkit/Zepto have it)."""
    if not raw_json:
        return None
    try:
        import json
        data = json.loads(raw_json)
        if isinstance(data, dict):
            v = data.get("rating")
            if v is not None:
                try:
                    r = float(str(v))
                    return r if r >= 0 else None
                except (ValueError, TypeError):
                    pass
    except Exception:
        pass
    return None


def _mk_row(app, store_id, sku_key, name, price, mrp, in_stock, url,
            collections, category, raw_json, source, inv_db):
    # use_llm=False on this hot path: the union layer parses up to 167k names
    # per load — a per-row Ollama HTTP call (15s timeout) would hang any box
    # with Ollama up and violates offline-degradable. LLM refinement is
    # opt-in for eval/sample paths, not bulk parsing.
    p = parse_name(name, price, category, use_llm=False)
    # 'collections' = SOURCE/APP taxonomy (verbatim); 'category' = INTERNAL
    # taxonomy (from the arg if present, else our categorize()).
    internal_cat = category if category else categorize(name)
    return {
        "app": app,
        "store_id": store_id,
        "sku_key": sku_key,
        "name": name,
        "price": price,
        "mrp": mrp,
        "in_stock": in_stock,
        "url": url or "",
        "collections": collections or "",        # SOURCE/APP taxonomy (verbatim)
        "category": internal_cat,                # INTERNAL taxonomy
        "raw_json": raw_json,
        "source": source,                        # 'inventory' | 'catalog'
        "inventory_db": inv_db,                  # provenance: which file holds raw_json
        # parsed grocery fields (Phase 1)
        "brand": p["brand"],
        "pack_value": p["pack_value"],
        "pack_unit": p["pack_unit"],
        "is_multipack": p["is_multipack"],
        "variant": p["variant"],
        "unit_base": p["unit_base"],
        "unit_price": p["unit_price"],
        "parse_status": p["parse_status"],
        "parse_source": p.get("parse_source"),   # regex | reference:<ds> | none
        "llm_used": p.get("llm_used", False),
        "rating": _extract_rating(raw_json),
    }


def load_product_space(apps=None, since=None, attach_embeddings=False, root=None):
    """Read-only union of all apps' product records. Returns list[dict].

    `root` overrides the repo root (used by tests). `attach_embeddings` lazily
    joins semantic vectors from deals.db when present (default off — vectors are
    an M2 concern and may be large; the semantic fallback in grouping degrades
    gracefully without them)."""
    root = root or _root()
    apps = [a.strip().lower() for a in (apps or APPS) if a.strip()]
    rows = []

    # 1) rich capture from per-app inventory DBs (primary product list)
    for app in apps:
        dbp = os.path.join(root, "inventory", f"inventory_{app}.db")
        if not os.path.exists(dbp):
            continue
        con = sqlite3.connect(f"file:{os.path.abspath(dbp)}?mode=ro", uri=True)
        try:
            if not _table_exists(con, "inventory_catalog"):
                continue
            for r in con.execute(
                "SELECT app,store_id,sku_key,name,price,mrp,in_stock,url,"
                "collections,category,raw_json FROM inventory_catalog"
            ):
                rows.append(_mk_row(app, r[1], r[2], r[3], r[4], r[5], r[6], r[7],
                                    r[8], r[9], r[10], "inventory", dbp))
        finally:
            con.close()

    # 2) deals.db catalog_snapshots (coverage where the rich path hasn't run yet),
    #    de-duplicated to the latest snapshot per (app,store,sku).
    deals = os.path.join(root, "deals.db")
    if os.path.exists(deals):
        con = sqlite3.connect(f"file:{os.path.abspath(deals)}?mode=ro", uri=True)
        try:
            seen = {(r["app"], r["store_id"], r["sku_key"]) for r in rows}
            q = (
                "SELECT cs.app,cs.store_id,cs.sku_key,cs.name,cs.price,cs.in_stock,"
                "cs.collections FROM catalog_snapshots cs WHERE (cs.app,cs.store_id,"
                "cs.sku_key,cs.ts) IN (SELECT app,store_id,sku_key,MAX(ts) FROM "
                "catalog_snapshots GROUP BY app,store_id,sku_key)"
            )
            params = []
            if since:
                q += " AND cs.ts >= ?"
                params.append(float(since))
            for r in con.execute(q, params):
                key = (r[0], r[1], r[2])
                if key in seen:
                    continue
                rows.append(_mk_row(r[0], r[1], r[2], r[3], r[4], None, r[5], "",
                                    r[6], None, None, "catalog", deals))
        finally:
            con.close()

    # 3) voucher exclusion (downstream consumers decide relevance; capture kept full)
    rows = [r for r in rows if not is_voucher_name(r["name"])]

    # 4) optional semantic vectors (M2; default off)
    if attach_embeddings:
        _attach_embeddings(rows, deals)

    # 5) entity-resolution cross-app identity
    assign_product_groups(rows)
    return rows


def _attach_embeddings(rows, deals_db_path, cap=4000):
    names = list({r["name"] for r in rows if r["name"]})
    if not names or len(names) > cap:
        if len(names) > cap:
            log.warning("product_space: %d distinct names > cap %d; skipping "
                        "semantic attach", len(names), cap)
        return
    con = sqlite3.connect(f"file:{os.path.abspath(deals_db_path)}?mode=ro", uri=True)
    try:
        model = None
        for m in _EMBED_MODELS:
            if con.execute("SELECT 1 FROM embeddings WHERE model=? LIMIT 1",
                           (m,)).fetchone():
                model = m
                break
        if not model:
            return
        dims = _dims_for(con, model)
        ph = ",".join("?" * len(names))
        vec_by_name = {}
        for name, blob in con.execute(
            f"SELECT name, vec FROM embeddings WHERE model=? AND dims=? "
            f"AND name IN ({ph})", [model, dims] + names
        ):
            vec_by_name[name] = _blob_to_vec(blob)
        for r in rows:
            if r["name"] in vec_by_name:
                r["semantic_vector"] = vec_by_name[r["name"]]
    finally:
        con.close()


def _cos(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def _sim(a_toks, b_toks):
    """Symmetric token-overlap fraction (== max(match_score(a,b), match_score(b,a))),
    but over precomputed token frozensets so the row/rep tokens are tokenized ONCE
    per grouping pass instead of once per candidate. Hot-path for 100k+ rows."""
    if not a_toks or not b_toks:
        return 0.0
    inter = len(a_toks & b_toks)
    if not inter:
        return 0.0
    return max(inter / len(a_toks), inter / len(b_toks))


def assign_product_groups(rows):
    """Entity-resolution grouping. Mutates rows in place, adding
    product_group_id / group_method / group_confidence. See module docstring for
    the hierarchy (exact -> matcher candidates -> pack/variant veto -> semantic).

    ponytail: exact canonical key is O(1) via `by_exact` (NOT a linear scan over
    all groups — at 100k+ rows a per-row scan is O(n*groups) and never finishes).
    Fuzzy candidate generation stays bounded per brand via `by_brand`."""
    groups = []                 # {gid, brand, norm, pack_value, pack_unit, variant, rep_name, rep_vector, ek}
    by_brand = {}               # brand_lower -> list of group indices
    by_exact = {}               # exact canonical key -> group index

    def _exact_key(b, n, v, u):
        return f"{b}|{n}|{v}|{u}"

    for r in rows:
        brand = (r["brand"] or "").lower()
        norm = _norm_product_name(r["name"])
        pv, pu = r["pack_value"], (r["pack_unit"] or "")
        variant = (r["variant"] or "").lower()
        r["_norm"], r["_brand_l"], r["_pv"], r["_pu"], r["_variant"] = norm, brand, pv, pu, variant
        rtoks = frozenset(_tokens(r["name"]))   # tokenized once per row

        # 1) exact canonical key (O(1) hashtable lookup)
        matched = None
        method, conf = None, 1.0
        ek = _exact_key(brand, norm, pv, pu)
        gi = by_exact.get(ek)
        if gi is not None:
            matched = groups[gi]["gid"]
            method = "exact"

        if matched is None:
            # 2) candidate generation within same brand (matcher as generator)
            cands = []
            for gi in by_brand.get(brand, []):
                g = groups[gi]
                s = _sim(rtoks, g["_toks"])
                if s >= CANDIDATE_THRESHOLD:
                    cands.append((s, g))
            # 3) pack-size / variant VETO — unknown pack/variant must also
            # veto on the fuzzy path: a no-pack row landing in a packed group
            # (or "Amul Milk" into "Amul Chocolate Milk") over-merges. Only
            # the exact canonical key (checked above) may join unknowns.
            survivors = []
            for s, g in cands:
                if (g["pack_value"] is None) != (pv is None):
                    continue  # one side unknown, other known -> no fuzzy join
                if pv is not None and (g["pack_value"] != pv or g["pack_unit"] != pu):
                    continue
                if g["variant"] or variant:  # either side set -> must agree
                    if not (g["variant"] and variant and g["variant"] == variant):
                        continue
                survivors.append((s, g))
            if survivors:
                survivors.sort(key=lambda x: -x[0])
                s, g = survivors[0]
                method, conf = "fuzzy", round(s, 3)
                # 4) semantic confirmation (never overrides a veto)
                sv, gv = r.get("semantic_vector"), g.get("rep_vector")
                if sv and gv and _cos(sv, gv) >= SEM_GROUP_THRESH:
                    method, conf = "semantic", round(_cos(sv, gv), 3)
                matched = g["gid"]

        if matched is None:
            gid = f"G{len(groups) + 1}"
            groups.append({"gid": gid, "brand": brand, "norm": norm,
                           "pack_value": pv, "pack_unit": pu, "variant": variant,
                           "rep_name": r["name"], "rep_vector": r.get("semantic_vector"),
                           "ek": ek, "_toks": rtoks})
            by_brand.setdefault(brand, []).append(len(groups) - 1)
            by_exact[ek] = len(groups) - 1
            matched, method, conf = gid, "new", 1.0

        r["product_group_id"] = matched
        r["group_method"] = method
        r["group_confidence"] = conf
    return rows, groups


if __name__ == "__main__":
    # Curated offline self-test (no network, no real DB required).
    sample = [
        # cross-app: same product on two apps -> ONE group
        _mk_row("blinkit", "B1", "k1", "Amul Taaza Toned Milk 500ml", 29.0, 33.0,
                1, "https://b/x", "home,Milk", "Dairy & Eggs", '{"id":"k1"}',
                "inventory", "inv_blinkit.db"),
        _mk_row("zepto", "Z1", "z9", "Amul Taaza Milk 500 ml", 30.0, 33.0,
                1, "https://z/x", "home", "Dairy & Eggs", '{"id":"z9"}',
                "inventory", "inv_zepto.db"),
        # different pack -> SEPARATE group (pack veto)
        _mk_row("blinkit", "B1", "k2", "Amul Taaza Toned Milk 1L", 55.0, 60.0,
                1, "https://b/y", "home,Milk", "Dairy & Eggs", '{"id":"k2"}',
                "inventory", "inv_blinkit.db"),
        # voucher -> excluded
        _mk_row("blinkit", "B1", "k3", "Steam Wallet Gift Card 500", 500.0, 500.0,
                1, "", "home", "Other", None, "inventory", "inv_blinkit.db"),
    ]
    rows = [r for r in sample if not is_voucher_name(r["name"])]
    assign_product_groups(rows)
    g500 = [r for r in rows if "500ml" in r["name"] or "500 ml" in r["name"]]
    g1l = [r for r in rows if "1L" in r["name"]]
    assert len({r["product_group_id"] for r in g500}) == 1, "500ml SKUs should share a group"
    assert len({r["product_group_id"] for r in g1l}) == 1, "1L should be its own group"
    assert g500[0]["product_group_id"] != g1l[0]["product_group_id"], "pack veto failed"
    for r in rows:
        assert r["app"] and r["store_id"] and r["sku_key"] and r["raw_json"], "provenance fields missing"
    print(f"[product_space] self-test OK: {len(rows)} rows, groups="
          f"{len({r['product_group_id'] for r in rows})}, 500ml cross-app merged, "
          f"1L separate (pack veto enforced)")
