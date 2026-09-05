#!/usr/bin/env python3
"""3D embedding map of the inventory.

Reads every product-name vector for one model from deals.db `embeddings`,
reduces 2048-D -> 3-D via PCA, clusters (k-means) for color grouping, and
writes a single self-contained interactive HTML (plotly WebGL scatter).

Run:  .venv/bin/python scripts/embed_3d_map.py [--model M] [--k N] [--out FILE]

ponytail: PCA-3 axes (not UMAP/t-SNE) — one dep-light path, minutes not
hours at 44k points; clusters still carry the semantic grouping. Upgrade
path if the map looks smeared: PCA->50 then UMAP->3.
"""
import argparse
import os
import sqlite3
import sys

import numpy as np

DB = os.path.join(os.path.dirname(__file__), "..", "deals.db")
DEFAULT_MODEL = "nvidia/llama-nemotron-embed-vl-1b-v2"


def load_vectors(db_path, model):
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT name, vec, dims FROM embeddings WHERE model = ?", (model,)
    ).fetchall()
    conn.close()
    if not rows:
        sys.exit(f"no embeddings rows for model {model!r} in {db_path}")
    names = [r[0] for r in rows]
    dims = rows[0][2]
    mat = np.empty((len(rows), dims), dtype=np.float32)
    for i, (_, blob, d) in enumerate(rows):
        assert d == dims, f"mixed dims in table: {d} vs {dims}"
        v = np.frombuffer(blob, dtype="<f4")
        assert v.shape == (dims,), f"row {i}: blob {len(blob)}B != {dims} f4s"
        mat[i] = v
    assert np.isfinite(mat).all(), "non-finite values in vectors"
    return names, mat


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--k", type=int, default=24, help="k-means cluster count")
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(__file__), "..", "exports", "embedding_map.html"))
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()

    print(f"loading {args.model} ...")
    names, mat = load_vectors(args.db, args.model)
    n, d = mat.shape
    print(f"loaded {n} vectors x {d} dims")

    # unit-normalize: these models are compared by cosine; PCA on raw
    # magnitudes would cluster by vector norm instead of direction.
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    mat = mat / norms

    from sklearn.decomposition import PCA
    from sklearn.cluster import KMeans

    mid = min(50, d, n)
    print(f"PCA {d} -> {mid} (denoise) ...")
    pca_mid = PCA(n_components=mid, random_state=0).fit(mat)
    low = pca_mid.transform(mat).astype(np.float32)

    print(f"k-means k={args.k} ...")
    km = KMeans(n_clusters=args.k, n_init=4, random_state=0).fit(low)
    labels = km.labels_

    print("PCA -> 3 for display ...")
    pca3 = PCA(n_components=3, random_state=0).fit(low)
    xyz = pca3.transform(low)
    evr = pca3.explained_variance_ratio_
    print(f"3D explained variance: {sum(evr):.1%} "
          f"({evr[0]:.1%} / {evr[1]:.1%} / {evr[2]:.1%})")
    sizes = np.bincount(labels, minlength=args.k)
    print("cluster sizes:", sizes.tolist())

    # one readable label per cluster: 3 names nearest its centroid
    names_arr = np.array(names)
    cluster_label = {}
    for c in range(args.k):
        idx = labels == c
        dists = np.linalg.norm(low[idx] - km.cluster_centers_[c], axis=1)
        top = np.flatnonzero(idx)[np.argsort(dists)[:3]]
        cluster_label[c] = ", ".join(names_arr[top])

    import plotly.graph_objects as go
    fig = go.Figure()
    order = np.argsort(-sizes)  # big clusters first in the legend
    for c in order:
        idx = labels == c
        fig.add_trace(go.Scatter3d(
            x=xyz[idx, 0], y=xyz[idx, 1], z=xyz[idx, 2],
            mode="markers",
            name=f"c{c} · {cluster_label[c]} ({int(sizes[c])})",
            text=np.array(names)[idx],
            hoverinfo="text",
            marker=dict(size=1.6, opacity=0.7),
        ))
    fig.update_layout(
        title=(f"Inventory embedding map — {n} products, {args.model}, "
               f"{args.k} clusters (PCA of cosine-normalized vectors)"),
        scene=dict(xaxis_title="PC1", yaxis_title="PC2", zaxis_title="PC3"),
        legend=dict(font=dict(size=9)),
    )
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    fig.write_html(args.out, include_plotlyjs=True, full_html=True)
    print(f"wrote {os.path.abspath(args.out)}")


if __name__ == "__main__":
    main()
