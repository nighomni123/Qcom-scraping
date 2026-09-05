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

PAGE_TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<style>
html, body {height: 100%; margin: 0; font-family: system-ui, sans-serif;}
#wrap {display: flex; height: 100%;}
#main {flex: 1; position: relative; min-width: 0;}
#panel {width: 340px; display: none; flex-direction: column;
        border-left: 1px solid #ddd; background: #fafafa;}
#panel.open {display: flex;}
#panel header {padding: 8px 10px; font-size: 12px; color: #555;
        border-bottom: 1px solid #ddd;}
#list {overflow-y: auto; flex: 1; padding: 6px 10px; font-size: 13px;}
#list .row {margin-bottom: 8px;}
#list .nm {font-weight: 600;}
#list .pr {color: #1a7f37;}
#list .empty {color: #999; padding-top: 12px;}
#ctrl {position: absolute; top: 4px; left: 12px; z-index: 10;
       background: rgba(255,255,255,.85); padding: 4px 8px;
       border-radius: 6px; font-size: 13px; border: 1px solid #ddd;}
</style>
</head>
<body>
<div id="wrap">
  <div id="main">
    <div id="ctrl"><label><input type="checkbox" id="nbrToggle">
      Hover neighborhood panel</label></div>
    __GRAPH__
  </div>
  <div id="panel">
    <header>Products near cursor <span id="nbrCount"></span></header>
    <div id="list"><div class="empty">Move the mouse over the map…</div></div>
  </div>
</div>
<script>
(function () {
  var NAMES = __NAMES__;
  var PRICES = __PRICES__;
  var R = __R__;
  // decode xyz coords (little-endian float32, n x 3 — all browsers are LE)
  var bytes = Uint8Array.from(atob("__B64__"), function (c) { return c.charCodeAt(0); });
  var f32 = new Float32Array(bytes.buffer);
  var N = f32.length / 3;

  var panel = document.getElementById('panel');
  var list = document.getElementById('list');
  var count = document.getElementById('nbrCount');
  var gd = document.getElementById('map');

  document.getElementById('nbrToggle').addEventListener('change', function () {
    panel.classList.toggle('open', this.checked);
    setTimeout(function () { Plotly.Plots.resize(gd); }, 50);  // fit new width
  });

  var lastT = 0;
  function showNeighbors(px, py, pz) {
    if (!panel.classList.contains('open')) return;
    var r2 = R * R;
    var html = [];
    for (var i = 0; i < N; i++) {
      var dx = f32[i*3] - px, dy = f32[i*3+1] - py, dz = f32[i*3+2] - pz;
      if (dx*dx + dy*dy + dz*dz <= r2) {
        html.push('<div class="row"><div class="nm">' + esc(NAMES[i]) +
                  '</div>' + (PRICES[i] ? '<div class="pr">' + esc(PRICES[i]) +
                  '</div>' : '') + '</div>');
        if (html.length >= 200) break;
      }
    }
    count.textContent = html.length ? '(' + Math.min(html.length, 200) +
      (html.length >= 200 ? '+' : '') + ')' : '';
    list.innerHTML = html.length ? html.join('') :
      '<div class="empty">No products within radius ' + R.toFixed(2) +
      ' of cursor.</div>';
  }
  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];
    });
  }
  // plotly hover events give us the 3D point under the cursor
  gd.addEventListener('plotly_hover', function (ev) {
    var now = Date.now();
    if (now - lastT < 80) return;  // throttle to ~12 updates/s
    lastT = now;
    var pt = ev.points[0];
    showNeighbors(pt.x, pt.y, pt.z);
  });
  gd.addEventListener('plotly_unhover', function () {
    // keep the last list; clearing on every leave flickers too much
  });
})();
</script>
</body>
</html>
"""


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

    # latest price_obs per (name, app, store) -> distinct price list per name
    conn = sqlite3.connect(args.db)
    prows = conn.execute("""
        SELECT name, GROUP_CONCAT(price) FROM (
            SELECT name, printf('%.0f', price) price,
                   ROW_NUMBER() OVER (PARTITION BY name, app, store_id
                                      ORDER BY ts DESC) rn
            FROM price_obs WHERE price IS NOT NULL
        ) WHERE rn = 1 GROUP BY name
    """).fetchall()
    conn.close()
    price_map = {nm: "₹" + ", ₹".join(sorted(set(p.split(",")), key=float))
                 for nm, p in prows}
    prices_arr = [price_map.get(nm, "") for nm in names]
    print(f"prices joined for {sum(1 for p in prices_arr if p)}/{n} names")

    import base64
    import json
    import plotly.graph_objects as go
    fig = go.Figure()
    order = np.argsort(-sizes)  # big clusters first in the legend
    for c in order:
        idx = labels == c
        fig.add_trace(go.Scatter3d(
            x=xyz[idx, 0], y=xyz[idx, 1], z=xyz[idx, 2],
            mode="markers",
            name=f"c{c} · {cluster_label[c]} ({int(sizes[c])})",
            text=names_arr[idx],
            hoverinfo="text",
            marker=dict(size=1.6, opacity=0.7),
        ))
    fig.update_layout(
        title=(f"Inventory embedding map — {n} products, {args.model}, "
               f"{args.k} clusters (PCA of cosine-normalized vectors)"),
        scene=dict(xaxis_title="PC1", yaxis_title="PC2", zaxis_title="PC3"),
        legend=dict(font=dict(size=9)),
        margin=dict(l=0, r=0, t=40, b=0),
        hoverdistance=40,  # panel follows the mouse through sparse gaps too
    )

    graph = fig.to_html(full_html=False, include_plotlyjs="inline", div_id="map")
    # neighborhood radius: 2% of the point-cloud diagonal
    radius = 0.02 * float(np.linalg.norm(xyz.max(0) - xyz.min(0)))
    page = (PAGE_TEMPLATE
            .replace("__GRAPH__", graph)
            .replace("__B64__", base64.b64encode(
                xyz.astype("<f4").tobytes()).decode())
            .replace("__NAMES__", json.dumps(names, ensure_ascii=False))
            .replace("__PRICES__", json.dumps(prices_arr, ensure_ascii=False))
            .replace("__R__", repr(radius)))
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"wrote {os.path.abspath(args.out)}")


if __name__ == "__main__":
    main()
