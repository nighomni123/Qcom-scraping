#!/usr/bin/env python3
"""
Compare embeddinggemma (local, 300M) vs NVIDIA nemotron on deals.db.

Both vector sets already live in the `embeddings` table, keyed by (model,dims):
    embeddinggemma : model='embeddinggemma',                            dims=768
    nemotron       : model='nvidia/llama-nemotron-embed-vl-1b-v2',     dims=2048
For a sample of product names present in BOTH sets, we compute each model's
top-k nearest neighbours within its OWN vector space, then measure how much the
two ranked lists agree (Overlap@k / Jaccard). This is a network-free quality
test: does the local 300M model reproduce the structure of the hosted 38k-name
reference? High agreement => embeddinggemma is a solid local drop-in.

Uses numpy if available (fast matrix cosine); falls back to pure Python.

Usage:
    .venv/bin/python scripts/compare_embeddings.py [--k 10] [--anchors 40]
"""
from __future__ import annotations

import argparse
import array
import os
import random
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

try:
    import numpy as np
    HAVE_NP = True
except Exception:
    from src import embed as E  # noqa: F401  (pure-python cosine fallback)
    HAVE_NP = False

GEMMA = ("embeddinggemma", 768)
NEMO = ("nvidia/llama-nemotron-embed-vl-1b-v2", 2048)


def _iter_rows(db, model, dims):
    conn = sqlite3.connect(db)
    try:
        cur = conn.execute(
            "SELECT name, vec FROM embeddings WHERE model=? AND dims=?",
            (model, dims))
        for nm, blob in cur:
            v = array.array("f")
            v.frombytes(blob)
            yield nm, v
    finally:
        conn.close()


def load(db, model, dims):
    names, vecs = [], []
    for nm, v in _iter_rows(db, model, dims):
        names.append(nm)
        vecs.append(v)
    if HAVE_NP:
        M = np.array(vecs, dtype=np.float32)
        M /= (np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)
        return names, M
    return names, vecs


def topk(db, names, M, q, k, exclude):
    if HAVE_NP:
        qs = q / (np.linalg.norm(q) + 1e-9)
        if hasattr(M, "numpy"):
            qs = qs.numpy()
        scores = M @ qs
        ex = names.index(exclude) if exclude in names else -1
        if ex >= 0:
            scores[ex] = -1e9
        idx = np.argpartition(-scores, min(k, len(scores) - 1))[:k]
        idx = idx[np.argsort(-scores[idx])]
        return [names[i] for i in idx]
    # pure-python fallback
    from src import embed as E
    qn = E._norm(q)
    out = []
    for nm, v in zip(names, M):
        if nm == exclude:
            continue
        out.append((E._cos(q, qn, v), nm))
    out.sort(key=lambda t: (-t[0], t[1]))
    return [nm for _, nm in out[:k]]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--anchors", type=int, default=40)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--db", default=os.path.join(REPO, "deals.db"))
    args = ap.parse_args()

    print(f"[compare] backend: {'numpy' if HAVE_NP else 'pure-python'}", flush=True)
    g_names, g_M = load(args.db, *GEMMA)
    n_names, n_M = load(args.db, *NEMO)
    shared = sorted(set(g_names) & set(n_names))
    print(f"[compare] gemma={len(g_names)}  nemotron={len(n_names)}  "
          f"shared={len(shared)}", flush=True)
    if len(shared) < 2:
        print("ERROR: not enough shared names yet — let the backfill progress.",
              file=sys.stderr)
        sys.exit(1)

    rng = random.Random(args.seed)
    anchors = rng.sample(shared, min(args.anchors, len(shared)))

    overlaps, jaccards = [], []
    print(f"\n{'='*78}\nNEIGHBOUR AGREEMENT  (Overlap@{args.k}: fraction of "
          f"gemma's top-{args.k} also in nemotron's top-{args.k})\n{'='*78}")
    for a in anchors:
        gv = next(v for nm, v in zip(g_names, g_M) if nm == a) if HAVE_NP else \
            next(v for nm, v in zip(g_names, g_M) if nm == a)
        nv = next(v for nm, v in zip(n_names, n_M) if nm == a)
        g_top = topk(args.db, g_names, g_M, gv, args.k, a)
        n_top = topk(args.db, n_names, n_M, nv, args.k, a)
        gs, ns = set(g_top), set(n_top)
        ov = len(gs & ns) / args.k
        jc = len(gs & ns) / len(gs | ns) if (gs | ns) else 0.0
        overlaps.append(ov)
        jaccards.append(jc)
        print(f"\n>> {a!r}   overlap@{args.k}={ov:.2f}  jaccard={jc:.2f}")
        for i in range(min(3, args.k)):
            g = g_top[i] if i < len(g_top) else "-"
            n = n_top[i] if i < len(n_top) else "-"
            print(f"    {i+1}. gemma: {g!r:50.50}  nemotron: {n!r:50.50}")

    mean_ov = sum(overlaps) / len(overlaps)
    mean_jc = sum(jaccards) / len(jaccards)
    print(f"\n{'='*78}\nSUMMARY over {len(overlaps)} shared anchors "
          f"(k={args.k})\n{'='*78}")
    print(f"  mean Overlap@{args.k} : {mean_ov:.3f}")
    print(f"  mean Jaccard         : {mean_jc:.3f}")
    if mean_ov >= 0.6:
        verdict = "STRONG agreement — embeddinggemma is a solid local drop-in."
    elif mean_ov >= 0.4:
        verdict = "MODERATE agreement — usable for lift, worth calibrating."
    else:
        verdict = "WEAK agreement — check prefixes / dims before trusting."
    print(f"  verdict              : {verdict}")


if __name__ == "__main__":
    main()
