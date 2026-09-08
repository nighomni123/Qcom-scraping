#!/usr/bin/env python3
"""Inventory Atlas — semantic explorer for the product-embedding archive.

Reads every product-name vector for one model from deals.db `embeddings`,
reduces via PCA (2048-D -> 50-D -> 3-D + 2-D), clusters (k-means) for grouping,
joins cheapest-latest price/store/category per name, and writes a single
self-contained dark-themed HTML: 3D semantic map + 2D overview navigator +
cluster explorer + product inspector, with Semantic/Price/Store/Category/
Density view modes, plus the M2 Product-Space extensions: Unit Price and
Cross-App explore modes, and an Opportunity mode group (Gap / Opportunity)
rendered as distinct colors layered over the geometry with per-point marker
symbols in gap/opportunity modes (observed product 'circle', candidate gap
'circle-open', strong opportunity 'diamond', rejected 'x') and per-point
tooltips.

Run:  .venv/bin/python scripts/embed_3d_map.py [--model nemotron|gemma|M]
      [--k N] [--out FILE] [--db FILE] [--projection pca|umap] [--seed N]
      [--sample N] [--neighbors N] [--opp-json FILE]

--opp-json FILE: optional scored-opportunity snapshot (M6/M7 shape, JSON) that
switches the Opportunity modes from lightweight inference to real data.  The
file is a list of records, or an object with an "opportunities"/"results"/
"items" list.  Each record may carry: rep_name|name (product name to color),
product_group_id, score (0-100; >= OPP_STRONG_MIN = "strong opportunity"),
gap_types (["assortment", "emerging", ...]), kind (explicit: gap |
opportunity | emerging | assortment | rejected), rejected (bool), category,
coverage (0-1, fraction of tracked apps), dpi, gap_strength, provenance,
validation_reason_codes, note.  Without the file, only honest *inferred*
candidates are drawn (assortment subset + locally-sparse embedding cluster),
tagged "candidate (inferred)" in the tooltip and reported in the UI.  See
normalize_opportunities() for the full merge.

Unit Price mode colors points by the parsed unit price (rupees per kg/L/each,
via src.product_fields.parse_name + unit_price); where the pack is unknown
the point stays neutral grey with a "~unit price unknown" tooltip note, and
grey where no price exists.  Cross-App mode colors points by which tracked
apps (blinkit/zepto/instamart) carry the product: one app = that app's hue,
two = a pink blend, three = bright mint; carried-on-none stays grey.

ponytail: PCA-3/PCA-2 projections (not UMAP by default) — one dep-light path,
minutes not hours at 44k points; --projection umap upgrades to PCA-50 then
UMAP-3/UMAP-2 if the map looks smeared (needs umap-learn installed).
ponytail: cluster territories are 2D convex hulls on the overview map only —
a viewport-sync rectangle on the 2D map is deferred (needs camera-frustum
math); 2D click-to-focus covers navigation for v1.
"""
import argparse
import base64
import colorsys
import json
import os
import re
import sqlite3
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from src.categories import ALL_CATEGORIES, categorize  # noqa: E402
from src.product_fields import parse_name, unit_price  # noqa: E402

DB = os.path.join(os.path.dirname(__file__), "..", "deals.db")
DEFAULT_MODEL = "nvidia/llama-nemotron-embed-vl-1b-v2"
# opportunity score (0-1 scale) at or above this is a "strong opportunity"
OPP_STRONG_MIN = 0.35
# friendly aliases -> exact `embeddings.model` strings
MODEL_ALIASES = {
    "nemotron": DEFAULT_MODEL,
    "gemma": "embeddinggemma",
}

PAGE_TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Inventory Atlas</title>
<style>
:root {
  --bg: #0b1020; --bg2: #111830; --bg3: #18213c; --line: #26304d;
  --txt: #e8ecf6; --dim: #9aa4c0; --faint: #5d6884;
  --accent: #7dd3fc; --good: #4ade80;
}
* {box-sizing: border-box;}
html, body {height: 100%; margin: 0;}
body {background: var(--bg); color: var(--txt);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif; font-size: 13px;
  display: flex; flex-direction: column; overflow: hidden;}
header {display: flex; align-items: center; gap: 14px; padding: 10px 16px;
  border-bottom: 1px solid var(--line); background: var(--bg2); flex-wrap: wrap;}
header h1 {font-size: 16px; margin: 0; letter-spacing: .3px; white-space: nowrap;}
header .sub {color: var(--dim); font-size: 12px; white-space: nowrap;}
#searchWrap {position: relative; flex: 1; min-width: 200px; max-width: 460px;}
#search {width: 100%; padding: 7px 12px; border-radius: 8px; font-size: 13px;
  border: 1px solid var(--line); background: var(--bg); color: var(--txt);}
#search::placeholder {color: var(--faint);}
#results {position: absolute; top: 105%; left: 0; right: 0; z-index: 50;
  background: var(--bg3); border: 1px solid var(--line); border-radius: 8px;
  max-height: 260px; overflow-y: auto; display: none;}
#results.open {display: block;}
#results .hit {padding: 6px 10px; cursor: pointer; border-bottom: 1px solid var(--line);}
#results .hit:last-child {border-bottom: none;}
#results .hit:hover {background: #223052;}
#results .hit .p {color: var(--good); font-size: 12px;}
.modes {display: flex; gap: 4px; flex-wrap: wrap;}
.modes button {background: transparent; color: var(--dim); border: 1px solid var(--line);
  border-radius: 7px; padding: 5px 10px; font-size: 12px; cursor: pointer;}
.modes button.on {background: var(--accent); border-color: var(--accent); color: #06121f;
  font-weight: 600;}
main {flex: 1; display: flex; min-height: 0;}
#left {width: 250px; min-width: 200px; border-right: 1px solid var(--line);
  background: var(--bg2); display: flex; flex-direction: column; min-height: 0;}
#left .hd {padding: 8px 12px; font-size: 11px; letter-spacing: 1px; color: var(--faint);
  display: flex; justify-content: space-between; align-items: center;}
#left .hd button {background: none; border: none; color: var(--faint); cursor: pointer;
  font-size: 11px;}
#clusters {flex: 1; overflow-y: auto; padding: 0 6px 8px;}
.crow {display: flex; align-items: center; gap: 8px; padding: 6px 8px; border-radius: 7px;
  cursor: pointer;}
.crow:hover {background: var(--bg3);}
.crow.focus {background: var(--bg3); outline: 1px solid var(--line);}
.crow .dot {width: 10px; height: 10px; border-radius: 50%; flex: none;}
.crow .lb {flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap;}
.crow .lb small {display: block; color: var(--faint); font-size: 11px;}
.crow .ct {color: var(--dim); font-size: 11px; flex: none;}
.crow .eye {background: none; border: none; color: var(--faint); cursor: pointer;
  font-size: 12px; padding: 0 2px;}
.crow.off .lb, .crow.off .ct {opacity: .35;}
#center {flex: 1; display: flex; flex-direction: column; min-width: 0; min-height: 0;}
#mapWrap {flex: 1; position: relative; min-height: 0;}
#mapWrap > #map3d, #ovWrap > #map2d {position: absolute; inset: 0;}
#map3d {position: absolute; inset: 0;}
#sceneBtns {position: absolute; top: 10px; right: 12px; z-index: 10; display: flex;
  gap: 4px; flex-wrap: nowrap; justify-content: flex-end; max-width: 70%;}
#sceneBtns button, #sceneBtns label {background: rgba(17,24,48,.9); color: var(--dim);
  border: 1px solid var(--line); border-radius: 7px; padding: 4px 9px; font-size: 11px;
  cursor: pointer;}
#sceneBtns button:hover {color: var(--txt);}
#bottom {height: 218px; border-top: 1px solid var(--line); display: flex; min-height: 0;}
#ovWrap {width: 340px; min-width: 260px; position: relative; border-right: 1px solid var(--line);}
#map2d {position: absolute; inset: 0;}
#ovCap {position: absolute; left: 10px; top: 6px; font-size: 11px; color: var(--faint);
  letter-spacing: 1px; z-index: 5; pointer-events: none;}
#cdetail {flex: 1; padding: 10px 14px; overflow-y: auto; min-width: 0;}
#cdetail h3 {margin: 0 0 2px; font-size: 14px;}
#cdetail .meta {color: var(--dim); font-size: 12px; margin-bottom: 8px;}
#cdetail .reps {display: flex; gap: 8px; flex-wrap: wrap;}
#cdetail .rep {background: var(--bg3); border: 1px solid var(--line); border-radius: 7px;
  padding: 5px 9px; font-size: 12px; cursor: pointer; max-width: 260px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;}
#cdetail .rep:hover {border-color: var(--accent);}
#cdetail .rep .p {color: var(--good);}
#right {width: 300px; min-width: 240px; border-left: 1px solid var(--line);
  background: var(--bg2); overflow-y: auto; padding: 12px 14px;}
#right h2 {font-size: 11px; letter-spacing: 1px; color: var(--faint); margin: 0 0 8px;}
#insp .nm {font-size: 14px; font-weight: 650; margin-bottom: 2px;}
#insp .pr {color: var(--good); font-size: 16px; font-weight: 700; margin: 4px 0 8px;}
#insp table {border-collapse: collapse; width: 100%; font-size: 12px; margin-bottom: 10px;}
#insp td {padding: 3px 0; vertical-align: top;}
#insp td.k {color: var(--faint); width: 74px;}
#sim .srow {padding: 6px 8px; border-radius: 7px; cursor: pointer;}
#sim .srow:hover {background: var(--bg3);}
#sim .srow .p {color: var(--good); font-size: 12px;}
#sim .srow .d {color: var(--faint); font-size: 11px;}
.empty {color: var(--faint);}
details.about {margin-top: 12px; color: var(--dim); font-size: 12px;}
details.about summary {cursor: pointer; color: var(--faint);}
</style>
</head>
<body>
<header>
  <div><h1>Inventory Atlas</h1><div class="sub">__SUBTITLE__</div></div>
  <div id="searchWrap">
    <input id="search" type="text" autocomplete="off"
      placeholder="Search for a product (e.g. headphones, USB cable, protein bar&hellip;)" />
    <div id="results"></div>
  </div>
  <div class="modes" id="modes">
    <button data-m="semantic" class="on">Semantic</button><button data-m="price">Price</button><button data-m="store">Store</button><button data-m="category">Category</button><button data-m="density">Density</button><button data-m="unit_price">Unit Price</button><button data-m="cross_app">Cross-App</button><button data-m="gap">Gap</button><button data-m="opportunity">Opportunity</button>
  </div>
</header>
<main>
  <aside id="left">
    <div class="hd"><span>CLUSTERS</span><button id="sortBtn" title="toggle sort">SIZE &#8595;</button></div>
    <div id="clusters"></div>
  </aside>
  <section id="center">
    <div id="mapWrap">
      __DIV3D__
      <div id="sceneBtns">
        <button id="bOver">Overview</button><button id="bTop">Top</button>
        <button id="bFollow" title="toggle">Follow: on</button>
        <label title="toggle"><input type="checkbox" id="bLabels" checked /> Labels</label>
      </div>
    </div>
    <div id="bottom">
      <div id="ovWrap"><div id="ovCap">2D OVERVIEW</div>__DIV2D__</div>
      <div id="cdetail"><span class="empty">Click a cluster on the left or in the overview to see its representative products.</span></div>
    </div>
  </section>
  <aside id="right">
    <h2>PRODUCT INSPECTOR</h2>
    <div id="insp"><span class="empty">Click a product in the map, overview, or search to inspect it.</span></div>
    <h2 style="margin-top:14px">SIMILAR PRODUCTS <span id="simCount"></span></h2>
    <div id="sim"></div>
    <details class="about"><summary>About this map</summary>
    <p>Each point is a product. Nearby products are more semantically similar
    (embedding cosine similarity, reduced to 3D/2D for display). Colors show groups
    discovered from the embedding space.</p>
    <p>__ABOUTTECH__</p></details>
  </aside>
</main>
__JS3D__
__JS2D__
<script>
(function () {
  var NAMES = __NAMES__;
  var CLUSTERS = __CLUSTERS__;
  var COLORS = __COLORS__;
  var APPS = __APPS__;
  var CATS = __CATS__;
  var R = __R__;
  var K = CLUSTERS.length;
  function b64ToArr(b64, Type) {
    var bin = atob(b64), n = bin.length / Type.BYTES_PER_ELEMENT;
    var bytes = new Uint8Array(n * Type.BYTES_PER_ELEMENT);
    for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return new Type(bytes.buffer);
  }
  var OPP = __OPP_JSON__ && __OPP_JSON__.map ? __OPP_JSON__ : (function() { var arr = new Array(NAMES.length); for (var i=0;i<arr.length;i++) arr[i]=null; return arr; })();
  var C5 = b64ToArr("__C5__", Float32Array);   // n x 5: x,y,z,x2,y2
  var CLU = b64ToArr("__CLU__", Uint16Array);  // cluster id per point
  var PNUM = b64ToArr("__PNUM__", Float32Array); // cheapest latest price, -1 = none
  var APP = b64ToArr("__APP__", Uint8Array);   // index into APPS, 255 = unknown
  var APPMASK = b64ToArr("__APPMASK__", Uint8Array); // bitmask over APPS order, 0 = unknown
  var UPRICE = b64ToArr("__UPRICE__", Float32Array); // unit price (Rs/kg-L-each), NaN = unknown
  var CAT = b64ToArr("__CAT__", Uint16Array);  // index into CATS
  var STOCK = b64ToArr("__STOCK__", Int8Array); // 1 in stock, 0 out, -1 unknown
  var N = NAMES.length;
  function X(i) { return C5[i*5]; } function Y(i) { return C5[i*5+1]; }
  function Z(i) { return C5[i*5+2]; } function X2(i) { return C5[i*5+3]; }
  function Y2(i) { return C5[i*5+4]; }

  var state = { selectedIndex: null, selectedCluster: null, mode: "semantic",
    showLabels: true, follow: true, sortSize: true, hidden: {} };
  var gd3 = document.getElementById('map3d');
  var gd2 = document.getElementById('map2d');
  // overlay trace indices: fig3d = K clusters + neighbors + selected + labels;
  // fig2 = K clusters + K hulls + neighbors
  var T3NBR = K, T3SEL = K + 1, T3LBL = K + 2, T2NBR = 2 * K;
  var hullTrace = function (c) { return K + trace2[c]; };
  // per-cluster member lists in trace order (python adds xyz[idx] in global
  // order, so filtering globals in order reproduces each trace's point order)
  var members = [], trace3 = [], trace2 = [];
  (function () {
    var byClu = [];
    for (var c = 0; c < K; c++) byClu.push([]);
    for (var i = 0; i < N; i++) byClu[CLU[i]].push(i);
    // python emits cluster traces big-first; recover that order from sizes
    var order = CLUSTERS.map(function (cl, c) { return [cl.count, c]; })
      .sort(function (a, b) { return b[0] - a[0]; }).map(function (t) { return t[1]; });
    for (var t = 0; t < K; t++) {
      var c2 = order[t];
      members[c2] = byClu[c2]; trace3[c2] = t; trace2[c2] = t;
    }
  })();

  function esc(s) {
    var t = document.createElement('textarea');
    t.innerHTML = String(s == null ? "" : s);
    return t.value.replace(/[&<>"]/g, function (c) {
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];
    });
  }
  function fmtPrice(p) {
    if (!(p >= 0)) return "";
    return "\u20B9" + Math.round(p).toLocaleString('en-IN');
  }
  function inr(n) { return n.toLocaleString('en-IN'); }
  function appName(i) { return i === 255 ? "\u2014" : APPS[i]; }
  function stockTxt(s) { return s === 1 ? "Yes" : s === 0 ? "No" : "Unknown"; }

  // ---------- palette helpers ----------
  function hexRgb(h) {
    return [parseInt(h.slice(1,3),16), parseInt(h.slice(3,5),16), parseInt(h.slice(5,7),16)];
  }
  function rgba(h, a) { var c = hexRgb(h); return "rgba("+c[0]+","+c[1]+","+c[2]+","+a+")"; }
  function lerp(h1, h2, t) {
    var a = hexRgb(h1), b = hexRgb(h2);
    return "rgb("+Math.round(a[0]+(b[0]-a[0])*t)+","+Math.round(a[1]+(b[1]-a[1])*t)+","+Math.round(a[2]+(b[2]-a[2])*t)+")";
  }
  var APPC = {}, CATC = {};
  APPS.forEach(function (a, i) { APPC[i] = COLORS[i % COLORS.length]; });
  CATS.forEach(function (c, i) { CATC[i] = COLORS[(i * 5 + 2) % COLORS.length]; });
  var PLO = Math.log(10), PHI = Math.log(100000);
  function priceColor(p) {
    if (!(p >= 0)) return "#3a4155";
    var t = Math.min(1, Math.max(0, (Math.log(Math.max(p,1)) - PLO) / (PHI - PLO)));
    return lerp("#1d4ed8", "#facc15", t);
  }
  var densRank = null;  // per-point 0..1 density rank, computed lazily
  function densityRank() {
    if (densRank) return densRank;
    var G = 24, mnx=1e9, mxx=-1e9, mny=1e9, mxy=-1e9, mnz=1e9, mxz=-1e9, i;
    for (i = 0; i < N; i++) {
      var x=X(i), y=Y(i), z=Z(i);
      if (x<mnx)mnx=x; if (x>mxx)mxx=x; if (y<mny)mny=y; if (y>mxy)mxy=y; if (z<mnz)mnz=z; if (z>mxz)mxz=z;
    }
    var cells = new Map();
    var key = new Uint32Array(N);
    for (i = 0; i < N; i++) {
      var cx=Math.min(G-1, ((X(i)-mnx)/(mxx-mnx+1e-9)*G)|0);
      var cy=Math.min(G-1, ((Y(i)-mny)/(mxy-mny+1e-9)*G)|0);
      var cz=Math.min(G-1, ((Z(i)-mnz)/(mxz-mnz+1e-9)*G)|0);
      var k2 = (cx*G+cy)*G+cz; key[i]=k2; cells.set(k2,(cells.get(k2)||0)+1);
    }
    var counts = Array.from(cells.values()).sort(function(a,b){return a-b;});
    densRank = new Float32Array(N);
    for (i = 0; i < N; i++) {
      var c = cells.get(key[i]), lo=0, hi=counts.length;
      while (lo<hi) { var m=(lo+hi)>>1; if (counts[m]<c) lo=m+1; else hi=m; }
      densRank[i] = lo / Math.max(1, counts.length-1);
    }
    return densRank;
  }
  function pointColor(c, i) {
    var m = state.mode;
    if (m === "price") return priceColor(PNUM[i]);
    if (m === "store") return APP[i] === 255 ? "#3a4155" : APPC[APP[i]];
    if (m === "category") return CATC[CAT[i]];
    if (m === "density") return lerp("#1e1b4b", "#facc15", densityRank()[i]);
    if (m === "unit_price") return priceColor(UPRICE[i]);
    if (m === "cross_app") {
      var mask = APPMASK[i], n2 = 0, only = -1;
      for (var b = 0; b < APPS.length; b++) if (mask & (1 << b)) { n2++; only = b; }
      if (!n2) return "#3a4155";
      if (n2 === 1) return APPC[only];
      if (n2 === 2) return "#e879f9";
      return "#5eead4";  // 3+ apps = mint
    }
    if (m === "gap") return (OPP && OPP[i] && OPP[i].gap) ? (OPP[i].gap === "candidate" ? "#f0e68c" : "#ff7b00") : "#e0e0e0";  // candidate ◎ = warm; rejected = orange; none = gray (ponytail: full gap-mode needs loaded opportunity JSON)
    if (m === "opportunity") return (OPP && OPP[i] && OPP[i].opportunity) ? (OPP[i].opportunity === "strong" ? "#00e676" : "#ffeb3b") : "#e0e0e0";  // strong ◉ = green; other = yellow; none = gray
    return COLORS[c % COLORS.length];
  }
  function pointSymbol(i) {
    var o = OPP && OPP[i];
    if (!o) return "circle";
    var k = o.kind || o.gap;
    if (k === "rejected" || o.rejected) return "x";
    if (o.opportunity === "strong") return "diamond";
    if (k && k !== "observed") return "circle-open";
    return "circle";
  }
  function applyMode() {
    var glyph = (state.mode === "gap" || state.mode === "opportunity");
    for (var c = 0; c < K; c++) {
      var mem = members[c], cols = new Array(mem.length);
      for (var j = 0; j < mem.length; j++) cols[j] = pointColor(c, mem[j]);
      var vis = state.hidden[c] ? false : true;
      Plotly.restyle(gd3, {"marker.color": [cols], visible: [vis]}, [trace3[c]]);
      Plotly.restyle(gd2, {"marker.color": [cols], visible: [vis]}, [trace2[c]]);
      Plotly.restyle(gd2, {visible: [vis]}, [hullTrace(c)]);  // hull territory
      if (glyph) {
        var syms = new Array(mem.length);
        for (var s2 = 0; s2 < mem.length; s2++) syms[s2] = pointSymbol(mem[s2]);
        Plotly.restyle(gd3, {"marker.symbol": [syms]}, [trace3[c]]);
        Plotly.restyle(gd2, {"marker.symbol": [syms]}, [trace2[c]]);
      } else {
        Plotly.restyle(gd3, {"marker.symbol": ["circle"]}, [trace3[c]]);
        Plotly.restyle(gd2, {"marker.symbol": ["circle"]}, [trace2[c]]);
      }
    }
    if (!state.showLabels) Plotly.restyle(gd3, {visible: [false]}, [T3LBL]);
    else if (!state.hiddenAny) Plotly.restyle(gd3, {visible: [true]}, [T3LBL]);
  }

  // ---------- cluster explorer ----------
  var clusDiv = document.getElementById('clusters');
  function renderExplorer() {
    var list = CLUSTERS.slice().sort(function (a, b) {
      return state.sortSize ? b.count - a.count : (a.label < b.label ? -1 : 1);
    });
    var html = [];
    for (var s = 0; s < list.length; s++) {
      var cl = list[s], c = cl.id;
      html.push('<div class="crow' + (state.selectedCluster === c ? ' focus' : '') +
        (state.hidden[c] ? ' off' : '') + '" data-c="' + c + '">' +
        '<span class="dot" style="background:' + COLORS[c % COLORS.length] + '"></span>' +
        '<span class="lb">' + esc(cl.label) +
        (cl.cat ? '<small>' + esc(cl.cat) + '</small>' : '') + '</span>' +
        '<span class="ct">' + inr(cl.count) + '</span>' +
        '<button class="eye" data-c="' + c + '" title="toggle visibility">' +
        (state.hidden[c] ? '&#9673;' : '&#9679;') + '</button></div>');
    }
    clusDiv.innerHTML = html.join('');
  }
  clusDiv.addEventListener('click', function (ev) {
    var eye = ev.target.closest('.eye');
    if (eye) {
      var c = +eye.getAttribute('data-c');
      state.hidden[c] = !state.hidden[c];
      state.hiddenAny = Object.keys(state.hidden).some(function (k) { return state.hidden[k]; });
      applyMode(); renderExplorer(); return;
    }
    var row = ev.target.closest('.crow');
    if (row) focusCluster(+row.getAttribute('data-c'));
  });
  document.getElementById('sortBtn').addEventListener('click', function () {
    state.sortSize = !state.sortSize;
    this.innerHTML = state.sortSize ? 'SIZE &#8595;' : 'NAME &#8595;';
    renderExplorer();
  });

  // ---------- camera ----------
  function cam3() { return (gd3.layout.scene || {}).camera || {}; }
  function setCam(center, eye) {
    var c = center || ((cam3().center) || {});
    var e = eye || cam3().eye || {x: 1.7, y: -1.5, z: 1.0};
    Plotly.relayout(gd3, {"scene.camera": {eye: e, center: c, up: {x:0,y:0,z:1}}});
  }
  function focusXYZ(x, y, z) { if (state.follow !== false) setCam({x:x, y:y, z:z}); }
  var HOME = {eye: {x: 1.7, y: -1.5, z: 1.0}, center: {x: 0, y: 0, z: 0}};
  document.getElementById('bOver').addEventListener('click', function () {
    clearSelection(); setCam(HOME.center, HOME.eye);
  });
  document.getElementById('bTop').addEventListener('click', function () {
    setCam(undefined, {x: 0, y: 0, z: 2.6});
  });
  document.getElementById('bFollow').addEventListener('click', function () {
    state.follow = !state.follow;
    this.textContent = 'Follow: ' + (state.follow ? 'on' : 'off');
  });
  document.getElementById('bLabels').addEventListener('change', function () {
    state.showLabels = this.checked;
    Plotly.restyle(gd3, {visible: [this.checked]}, [T3LBL]);
  });

  // ---------- selection ----------
  var insp = document.getElementById('insp');
  var simDiv = document.getElementById('sim');
  var simCount = document.getElementById('simCount');
  var cdetail = document.getElementById('cdetail');
  function neighborsOf(i, cap) {
    var px=X(i), py=Y(i), pz=Z(i), r2=R*R, hits=[];
    for (var j = 0; j < N; j++) {
      if (j === i) continue;
      var dx=C5[j*5]-px, dy=C5[j*5+1]-py, dz=C5[j*5+2]-pz, d2=dx*dx+dy*dy+dz*dz;
      if (d2 <= r2) hits.push([d2, j]);
    }
    hits.sort(function (a,b) { return a[0]-b[0]; });
    return hits.slice(0, cap);
  }
  function select(i, focus) {
    state.selectedIndex = i;
    var c = CLU[i];
    state.selectedCluster = c;
    // halo: bright selected point + colored neighbors on both maps
    var hits = neighborsOf(i, __NB__);
    state.nbrList = [i];
    for (var kh = 0; kh < hits.length; kh++) state.nbrList.push(hits[kh][1]);
    var nx=[X(i)], ny=[Y(i)], nz=[Z(i)], nx2=[X2(i)], ny2=[Y2(i)];
    var nCols = [COLORS[c % COLORS.length]];
    for (var k = 0; k < hits.length; k++) {
      var j = hits[k][1];
      nx.push(X(j)); ny.push(Y(j)); nz.push(Z(j)); nx2.push(X2(j)); ny2.push(Y2(j));
      nCols.push(COLORS[CLU[j] % COLORS.length]);
    }
    Plotly.restyle(gd3, {x:[nx], y:[ny], z:[nz], "marker.color":[nCols]}, [T3NBR]);
    Plotly.restyle(gd3, {x:[[X(i)]], y:[[Y(i)]], z:[[Z(i)]]}, [T3SEL]);
    Plotly.restyle(gd2, {x:[nx2], y:[ny2], "marker.color":[nCols]}, [T2NBR]);
    // dim unrelated clusters, keep selected + focus readable
    for (var cc = 0; cc < K; cc++) {
      if (state.hidden[cc]) continue;
      var op = (cc === c) ? 0.85 : 0.07;
      Plotly.restyle(gd3, {"marker.opacity": [op]}, [trace3[cc]]);
      Plotly.restyle(gd2, {"marker.opacity": [op]}, [trace2[cc]]);
    }
    if (focus !== false) focusXYZ(X(i), Y(i), Z(i));
    // inspector
    var upTxt = (UPRICE[i] >= 0) ? "\u20B9" + Math.round(UPRICE[i]).toLocaleString('en-IN') + "/unit" : "~unit price unknown";
    var rows = '<table>' +
      '<tr><td class="k">Unit price</td><td>' + esc(upTxt) + '</td></tr>' +
      '<tr><td class="k">Category</td><td>' + esc(CATS[CAT[i]]) + '</td></tr>' +
      '<tr><td class="k">Store</td><td>' + esc(appName(APP[i])) + '</td></tr>' +
      '<tr><td class="k">In stock</td><td>' + stockTxt(STOCK[i]) + '</td></tr>' +
      '<tr><td class="k">Cluster</td><td>' + esc(CLUSTERS[c].label) + '</td></tr></table>';
    var why = '';
    if (OPP && OPP[i] && (state.mode === 'gap' || state.mode === 'opportunity')) {
      var o = OPP[i];
      var none = '(none stated)';
      var vr2 = (o.validation_reason_codes && o.validation_reason_codes.join) ? o.validation_reason_codes.join(', ') : (o.validation_reason_codes || none);
      var gt2 = (o.gap_types && o.gap_types.join) ? o.gap_types.join(', ') : (o.gap || none);
      why = '<h2>WHY THIS WAS FLAGGED</h2><div style="font-size:11px;color:var(--dim);line-height:1.35">' +
        '<b>Gap types/kind:</b> ' + esc(gt2) + ' / ' + esc(o.kind || o.gap || 'unknown') + '<br/>' +
        '<b>Opportunity score:</b> ' + (o.score != null ? o.score.toFixed(4) : none) + '<br/>' +
        '<b>Gap strength:</b> ' + (o.gap_strength != null ? esc(o.gap_strength) : none) + '<br/>' +
        '<b>DPI:</b> ' + (o.dpi != null ? esc(o.dpi) : none) + '<br/>' +
        '<b>Coverage:</b> ' + (o.coverage != null ? esc(o.coverage) : none) + '<br/>' +
        '<b>Validation codes:</b> ' + esc(vr2) + '<br/>' +
        '<b>Category:</b> ' + esc(o.category || CATS[CAT[i]]) + '<br/>' +
        '<b>Provenance:</b> ' + esc(o.provenance || none) +
        (o.note ? '<br/><b>Note:</b> ' + esc(o.note) : '') +
        '</div>';
    }
    insp.innerHTML = '<div class="nm">' + esc(NAMES[i]) + '</div>' +
      '<div class="pr">' + (fmtPrice(PNUM[i]) || '<span class="empty">price unknown</span>') + '</div>' + rows +
      (why ? '<div style="margin-top:12px;padding-top:10px;border-top:1px solid var(--line)">' + why + '</div>' : '');
    var sh = [];
    for (var k2 = 0; k2 < hits.length; k2++) {
      var j2 = hits[k2][1];
      sh.push('<div class="srow" data-i="' + j2 + '">' + esc(NAMES[j2]) +
        ' <span class="p">' + fmtPrice(PNUM[j2]) + '</span>' +
        ' <span class="d">d=' + Math.sqrt(hits[k2][0]).toFixed(2) + '</span></div>');
    }
    simCount.textContent = hits.length ? '(' + hits.length + ')' : '';
    simDiv.innerHTML = sh.length ? sh.join('') : '<span class="empty">No products within radius.</span>';
    renderExplorer(); renderClusterCard(c);
  }
  function clearSelection(restoreOp) {
    state.selectedIndex = null; state.selectedCluster = null; state.nbrList = null;
    Plotly.restyle(gd3, {x:[[]], y:[[]], z:[[]]}, [T3NBR]);
    Plotly.restyle(gd3, {x:[[]], y:[[]], z:[[]]}, [T3SEL]);
    Plotly.restyle(gd2, {x:[[]], y:[[]]}, [T2NBR]);
    if (restoreOp !== false) {
      for (var cc = 0; cc < K; cc++) {
        if (state.hidden[cc]) continue;
        Plotly.restyle(gd3, {"marker.opacity": [0.5]}, [trace3[cc]]);
        Plotly.restyle(gd2, {"marker.opacity": [0.65]}, [trace2[cc]]);
      }
    }
    insp.innerHTML = '<span class="empty">Click a product in the map, overview, or search to inspect it.</span>';
    simDiv.innerHTML = ''; simCount.textContent = '';
    renderExplorer();
  }
  simDiv.addEventListener('click', function (ev) {
    var row = ev.target.closest('.srow');
    if (row) select(+row.getAttribute('data-i'));
  });
  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape') { clearSelection(); renderClusterCard(null); }
  });

  function renderClusterCard(c) {
    if (c == null) {
      cdetail.innerHTML = '<span class="empty">Click a cluster on the left or in the overview to see its representative products.</span>';
      return;
    }
    var cl = CLUSTERS[c], rh = [];
    for (var k = 0; k < cl.reps.length; k++) {
      var j = cl.reps[k];
      rh.push('<span class="rep" data-i="' + j + '">' + esc(NAMES[j]) +
        ' <span class="p">' + fmtPrice(PNUM[j]) + '</span></span>');
    }
    cdetail.innerHTML = '<h3>' + esc(cl.label) + '</h3><div class="meta">' +
      inr(cl.count) + ' products' + (cl.cat ? ' &middot; mostly ' + esc(cl.cat) : '') + '</div>' +
      '<div class="reps">' + rh.join('') + '</div>';
  }
  cdetail.addEventListener('click', function (ev) {
    var rep = ev.target.closest('.rep');
    if (rep) select(+rep.getAttribute('data-i'));
  });

  function focusCluster(c) {
    if (state.selectedCluster === c && state.selectedIndex == null) {
      state.selectedCluster = null; renderExplorer(); renderClusterCard(null);
      for (var cc = 0; cc < K; cc++) {
        if (state.hidden[cc]) continue;
        Plotly.restyle(gd3, {"marker.opacity": [0.5]}, [trace3[cc]]);
        Plotly.restyle(gd2, {"marker.opacity": [0.65]}, [trace2[cc]]);
      }
      return;
    }
    state.selectedCluster = c;
    var cl = CLUSTERS[c];
    for (var cc2 = 0; cc2 < K; cc2++) {
      if (state.hidden[cc2]) continue;
      var op = (cc2 === c) ? 0.85 : 0.07;
      Plotly.restyle(gd3, {"marker.opacity": [op]}, [trace3[cc2]]);
      Plotly.restyle(gd2, {"marker.opacity": [op]}, [trace2[cc2]]);
    }
    setCam({x: cl.cx, y: cl.cy, z: cl.cz});
    renderExplorer(); renderClusterCard(c);
  }

  // ---------- plotly events ----------
  function traceIndexToGlobal(curve, point) {
    if (curve < K) {  // gd3 cluster trace t -> cluster c
      for (var c = 0; c < K; c++) if (trace3[c] === curve) return members[c][point];
    }
    return null;
  }
  function trace2ToGlobal(curve, point) {
    if (curve < K) {
      for (var c = 0; c < K; c++) if (trace2[c] === curve) return members[c][point];
    }
    return null;
  }
  if (gd3.on) {
    gd3.on('plotly_click', function (ev) {
      if (!ev.points.length) return;
      var pt = ev.points[0];
      if (pt.curveNumber === T3LBL) { focusCluster(pt.pointNumber); return; }
      if (pt.curveNumber === T3NBR || pt.curveNumber === T3SEL) {
        if (state.nbrList && pt.pointNumber < state.nbrList.length)
          select(state.nbrList[pt.pointNumber]);
        return;
      }
      var g = traceIndexToGlobal(pt.curveNumber, pt.pointNumber);
      if (g != null) select(g);
    });
  }
  if (gd2.on) {
    gd2.on('plotly_click', function (ev) {
      if (!ev.points.length) return;
      var pt = ev.points[0];
      if (pt.curveNumber === T2NBR) {
        if (state.nbrList && pt.pointNumber < state.nbrList.length)
          select(state.nbrList[pt.pointNumber]);
        return;
      }
      var g = trace2ToGlobal(pt.curveNumber, pt.pointNumber);
      if (g != null) { focusCluster(CLUSTERS[CLU[g]].id); select(g); }
    });
  }

  // ---------- search ----------
  var search = document.getElementById('search');
  var resBox = document.getElementById('results');
  var lastQ = "";
  search.addEventListener('input', function () {
    var q = search.value.trim().toLowerCase();
    lastQ = q;
    if (q.length < 2) { resBox.classList.remove('open'); resBox.innerHTML = ''; return; }
    var out = [], qi = 0;
    for (var i = 0; i < N && out.length < 20; i++) {
      if (NAMES[i].toLowerCase().indexOf(q) !== -1) out.push(i);
    }
    if (!out.length) {
      resBox.innerHTML = '<div class="hit empty">No matches.</div>';
    } else {
      var html = [];
      for (var k = 0; k < out.length; k++) {
        var j = out[k];
        html.push('<div class="hit" data-i="' + j + '">' + esc(NAMES[j]) +
          ' <span class="p">' + fmtPrice(PNUM[j]) + '</span></div>');
      }
      resBox.innerHTML = html.join('');
    }
    resBox.classList.add('open');
  });
  search.addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter') {
      var first = resBox.querySelector('.hit[data-i]');
      if (first) { select(+first.getAttribute('data-i')); resBox.classList.remove('open'); }
    } else if (ev.key === 'Escape') { resBox.classList.remove('open'); }
  });
  resBox.addEventListener('click', function (ev) {
    var hit = ev.target.closest('.hit[data-i]');
    if (hit) {
      select(+hit.getAttribute('data-i'));
      resBox.classList.remove('open'); search.value = NAMES[+hit.getAttribute('data-i')];
    }
  });
  document.addEventListener('click', function (ev) {
    if (!ev.target.closest('#searchWrap')) resBox.classList.remove('open');
  });

  // ---------- view modes ----------
  document.getElementById('modes').addEventListener('click', function (ev) {
    var b = ev.target.closest('button');
    if (!b) return;
    state.mode = b.getAttribute('data-m');
    var btns = this.querySelectorAll('button');
    for (var k = 0; k < btns.length; k++) btns[k].classList.toggle('on', btns[k] === b);
    // recolor selection overlays to match the mode too
    if (state.selectedIndex != null) { var s = state.selectedIndex; clearSelection(false); select(s, false); }
    applyMode();
  });

  window.addEventListener('resize', function () {
    Plotly.Plots.resize(gd3); Plotly.Plots.resize(gd2);
  });
  renderExplorer();
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


def normalize_vectors(mat):
    """Unit-normalize: these models are compared by cosine; PCA on raw
    magnitudes would cluster by vector norm instead of direction."""
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


def reduce_mid(mat, seed):
    from sklearn.decomposition import PCA

    n, d = mat.shape
    mid = min(50, d, n)
    print(f"PCA {d} -> {mid} (denoise) ...")
    pca_mid = PCA(n_components=mid, random_state=seed).fit(mat)
    return pca_mid.transform(mat).astype(np.float32)


def cluster_embeddings(low, k, seed):
    from sklearn.cluster import KMeans

    print(f"k-means k={k} ...")
    km = KMeans(n_clusters=k, n_init=4, random_state=seed).fit(low)
    return km.labels_, km.cluster_centers_


def project_space(low, n_components, projection, seed):
    if projection == "umap":
        try:
            import umap
        except ImportError:
            sys.exit("umap-learn is not installed (.venv has no umap module) — "
                     "re-run with --projection pca")
        print(f"UMAP {low.shape[1]} -> {n_components} ...")
        return umap.UMAP(n_components=n_components,
                         random_state=seed).fit_transform(low)
    from sklearn.decomposition import PCA

    print(f"PCA -> {n_components} for display ...")
    pca = PCA(n_components=n_components, random_state=seed).fit(low)
    out = pca.transform(low)
    if n_components == 3:
        evr = pca.explained_variance_ratio_
        print(f"3D explained variance: {sum(evr):.1%} "
              f"({evr[0]:.1%} / {evr[1]:.1%} / {evr[2]:.1%})")
    return out


def label_clusters(names, low, labels, centers, k):
    """One human-readable record per cluster: nearest-to-centroid
    representative names + a category-majority hint (no bare c0/c1 labels)."""
    names_arr = np.array(names)
    out = []
    for c in range(k):
        idx = np.flatnonzero(labels == c)
        if len(idx) == 0:
            out.append({"id": c, "label": f"cluster {c}", "count": 0,
                        "cat": "", "reps": []})
            continue
        dists = np.linalg.norm(low[idx] - centers[c], axis=1)
        top = idx[np.argsort(dists)[:3]]
        lbl = str(names_arr[top[0]])
        if len(lbl) > 55:
            lbl = lbl[:54] + "…"
        # category hint over a capped sample (categorize is O(rules) per name)
        samp = top if len(idx) <= 200 else idx[np.linspace(
            0, len(idx) - 1, 200).astype(int)]
        votes = {}
        for j in samp:
            cat = categorize(names[j])
            votes[cat] = votes.get(cat, 0) + 1
        best, nbest = max(votes.items(), key=lambda t: t[1])
        cat = best if best != "Other" and nbest >= 0.4 * len(samp) else ""
        out.append({"id": c, "label": lbl, "count": int(len(idx)),
                    "cat": cat, "reps": [int(t) for t in top]})
    return out


def load_product_meta(db_path, names):
    """Cheapest-latest price + its app per name, category (stored or
    inferred), and latest stock rollup via the watchlist name bridge.
    Missing data stays missing — never fabricated."""
    conn = sqlite3.connect(db_path)
    # latest row per (name, app, store) -> cheapest per name wins the inspector
    prows = conn.execute("""
        SELECT name, price, app, category FROM (
            SELECT name, price, app, category,
                   ROW_NUMBER() OVER (PARTITION BY name, app, store_id
                                      ORDER BY ts DESC) rn
            FROM price_obs WHERE price IS NOT NULL
        ) WHERE rn = 1
    """).fetchall()
    best = {}
    apps_by_name = {}
    for nm, price, app, cat in prows:
        if app:
            apps_by_name.setdefault(nm, set()).add(app)
        if price is None:
            continue
        prev = best.get(nm)
        if prev is None or price < prev[0]:
            best[nm] = (float(price), app or "", cat or "")
    # latest stock per sku bridged to names through the watchlist; a name is
    # "in stock" if ANY of its latest sightings is 1 (NULLs ignored by MAX)
    srows = conn.execute("""
        SELECT w.name, MAX(s.in_stock) FROM (
            SELECT app, store_id, sku_key, in_stock,
                   ROW_NUMBER() OVER (PARTITION BY app, store_id, sku_key
                                      ORDER BY ts DESC) rn
            FROM stock_obs
        ) s JOIN watchlist w ON w.app = s.app AND w.store_id = s.store_id
                              AND w.sku_key = s.sku_key
        WHERE s.rn = 1 GROUP BY w.name
    """).fetchall()
    conn.close()
    stock = {nm: (int(v) if v is not None else -1) for nm, v in srows}
    meta = {}
    for nm in names:
        b = best.get(nm)
        if b:
            price, app, cat = b
            cat = cat or categorize(nm)
        else:
            price, app, cat = -1.0, "", categorize(nm)
        meta[nm] = (price, app, cat, stock.get(nm, -1))
    print(f"prices joined for {sum(1 for nm in names if nm in best)}/{len(names)} names; "
          f"stock known for {sum(1 for nm in names if meta[nm][3] >= 0)} names")
    return meta, apps_by_name


def golden_palette(k, s=0.65, v=0.95):
    """k visually distinct hex colors (golden-angle hue rotation) — one source
    for 3D traces, 2D overview, explorer dots and mode overlays."""
    out = []
    for i in range(k):
        r, g, b = colorsys.hsv_to_rgb((i * 0.61803398875) % 1.0, s, v)
        out.append(f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}")
    return out


def cluster_hulls(xy2, labels, k, cap=1500, seed=0):
    """Convex-hull territory polygon per cluster over the 2D projection
    (subsampled; clusters that fail Qhull are simply skipped)."""
    try:
        from scipy.spatial import ConvexHull
    except Exception:
        print("scipy.spatial unavailable — skipping hull territories")
        return {}
    rng = np.random.default_rng(seed)
    out = {}
    for c in range(k):
        pts = xy2[labels == c]
        if len(pts) < 3:
            continue
        if len(pts) > cap:
            pts = pts[rng.choice(len(pts), cap, replace=False)]
        try:
            h = ConvexHull(pts)
        except Exception:
            continue
        out[c] = pts[h.vertices].tolist()
    return out


def b64(arr):
    return base64.b64encode(np.ascontiguousarray(arr).tobytes()).decode()


def normalize_opportunities(opp_json, names):
    """Index-aligned OPP array for the rendered names.

    Accepts either a pre-aligned list (one entry per rendered name, None
    for no data — used as-is) or scored-opportunity records: a list of
    dicts, or an object with an "opportunities"/"results"/"items" list,
    each record carrying rep_name|name, score (0-1, or 0-100 which is
    rescaled), gap_types, kind, rejected. Records are matched to rendered
    names case-insensitively; unmatched names stay None."""
    if isinstance(opp_json, list) and len(opp_json) == len(names) and all(
            o is None or (isinstance(o, dict) and ("gap" in o or "opportunity" in o))
            for o in opp_json):
        return opp_json
    recs = opp_json
    if isinstance(opp_json, dict):
        for key in ("opportunities", "results", "items"):
            if isinstance(opp_json.get(key), list):
                recs = opp_json[key]
                break
        else:
            return [None] * len(names)
    if not isinstance(recs, list):
        return [None] * len(names)
    by_name = {}
    for r in recs:
        if not isinstance(r, dict):
            continue
        nm = r.get("rep_name", r.get("name", ""))
        if nm:
            by_name.setdefault(str(nm).strip().lower(), r)
    out = []
    for nm in names:
        r = by_name.get(str(nm).strip().lower())
        if r is None:
            out.append(None)
            continue
        score = r.get("score")
        if isinstance(score, (int, float)) and score > 1:
            score = score / 100.0  # 0-100 scale -> 0-1
        gap_types = r.get("gap_types") or []
        if isinstance(gap_types, str):
            gap_types = [g.strip() for g in gap_types.split(",") if g.strip()]
        kind = r.get("kind")
        if r.get("rejected"):
            kind = "rejected"
        kind = kind or (gap_types[0] if gap_types else "candidate")
        vr = r.get("validation_reason_codes")
        if isinstance(vr, str):
            vr = [v.strip() for v in vr.split(",") if v.strip()]
        out.append({
            "gap": kind,
            "opportunity": ("strong" if isinstance(score, (int, float))
                            and score >= OPP_STRONG_MIN else "weak"),
            "score": score if isinstance(score, (int, float)) else None,
            "gap_types": list(gap_types) if gap_types else None,
            "kind": kind,
            "rejected": bool(r.get("rejected", False)),
            "coverage": r.get("coverage"),
            "dpi": r.get("dpi"),
            "gap_strength": r.get("gap_strength"),
            "provenance": r.get("provenance"),
            "validation_reason_codes": list(vr) if vr else None,
            "note": r.get("note"),
            "category": r.get("category"),
        })
    return out


def build_plotly_scene(names_arr, xyz, xy2, labels, clusters, colors,
                       hulls, radius):
    import plotly.graph_objects as go

    k = len(clusters)
    sizes = np.array([cl["count"] for cl in clusters])
    dark = dict(paper_bgcolor="#0b1020", plot_bgcolor="#0b1020",
                font=dict(color="#e8ecf6"))
    # --- 3D ---
    fig = go.Figure()
    order = np.argsort(-sizes)  # big clusters first; JS recovers this order
    for c in order:
        idx = labels == c
        fig.add_trace(go.Scatter3d(
            x=xyz[idx, 0], y=xyz[idx, 1], z=xyz[idx, 2],
            mode="markers",
            name=clusters[c]["label"],
            text=names_arr[idx],
            hoverinfo="text",
            showlegend=False,
            marker=dict(size=2.4, opacity=0.5, color=colors[c]),
        ))
    # 2D overview: same cluster colors, one trace per cluster (shared hide/
    # focus logic with the 3D scene) + hull territory polygons
    fig2 = go.Figure()
    for c in order:
        idx = labels == c
        fig2.add_trace(go.Scatter(
            x=xy2[idx, 0], y=xy2[idx, 1],
            mode="markers",
            name=clusters[c]["label"],
            text=names_arr[idx],
            hoverinfo="text",
            showlegend=False,
            marker=dict(size=3, opacity=0.65, color=colors[c]),
        ))
    for c in order:
        hull = hulls.get(c)
        if hull:
            hx = [p[0] for p in hull] + [hull[0][0]]
            hy = [p[1] for p in hull] + [hull[0][1]]
            r8, g8, b8 = (int(colors[c][1:3], 16), int(colors[c][3:5], 16),
                          int(colors[c][5:7], 16))
            fill = f"rgba({r8},{g8},{b8},0.07)"
        else:
            hx, hy, fill = [], [], "rgba(0,0,0,0)"
        fig2.add_trace(go.Scatter(
            x=hx, y=hy, mode="lines", fill="toself" if hull else "none",
            fillcolor=fill,
            line=dict(color=colors[c], width=1),
            hoverinfo="skip", showlegend=False,
            name=f"hull {c}",
        ))
    # selection overlays: 2D neighbors [T2NBR], 3D neighbors [T3NBR],
    # 3D selected point [T3SEL], 3D cluster labels [T3LBL] (see JS constants)
    fig2.add_trace(go.Scatter(
        x=[], y=[], mode="markers", hoverinfo="skip", showlegend=False,
        name="neighbors2d", marker=dict(size=6, opacity=1),
    ))
    fig.add_trace(go.Scatter3d(
        x=[], y=[], z=[], mode="markers", hoverinfo="skip", showlegend=False,
        name="neighbors3d", marker=dict(size=5, opacity=1),
    ))
    fig.add_trace(go.Scatter3d(
        x=[], y=[], z=[], mode="markers", hoverinfo="skip", showlegend=False,
        name="selected",
        marker=dict(size=10, color="#ffffff", opacity=1,
                    line=dict(color="#0b1020", width=2)),
    ))
    # in-scene cluster labels anchored at centroids
    cen = np.array([[cl["cx"], cl["cy"], cl["cz"]] for cl in clusters])
    fig.add_trace(go.Scatter3d(
        x=cen[:, 0], y=cen[:, 1], z=cen[:, 2],
        mode="text",
        text=[f"{cl['label']}<br>{cl['count']:,}" for cl in clusters],
        hoverinfo="skip", showlegend=False,
        name="labels",
        textfont=dict(size=9, color="#e8ecf6"),
    ))
    grid = "rgba(255,255,255,0.08)"
    fig.update_layout(
        **dark,
        scene=dict(
            xaxis=dict(showticklabels=False, title="", showgrid=True,
                       gridcolor=grid, zeroline=False,
                       backgroundcolor="#0b1020"),
            yaxis=dict(showticklabels=False, title="", showgrid=True,
                       gridcolor=grid, zeroline=False,
                       backgroundcolor="#0b1020"),
            zaxis=dict(showticklabels=False, title="", showgrid=True,
                       gridcolor=grid, zeroline=False,
                       backgroundcolor="#0b1020"),
            aspectmode="cube",
            camera=dict(eye=dict(x=1.7, y=-1.5, z=1.0),
                        up=dict(x=0, y=0, z=1),
                        projection=dict(type="perspective"))),
        margin=dict(l=0, r=0, t=0, b=0),
        hoverdistance=40,  # tooltip follows the mouse through sparse gaps too
    )
    fig2.update_layout(
        **dark,
        xaxis=dict(visible=False), yaxis=dict(visible=False,
                                              scaleanchor="x", scaleratio=1),
        margin=dict(l=4, r=4, t=4, b=4),
        hoverdistance=20,
    )
    g3 = fig.to_html(full_html=False, include_plotlyjs="inline", div_id="map3d")
    g2 = fig2.to_html(full_html=False, include_plotlyjs=False, div_id="map2d")
    return g3, g2, radius


def split_graph_div(graph_html, div_id):
    """plotly to_html wraps everything in a full-page sizing <div>:
    <div style="height:100%;width:100%">[scripts]<div id=...></div>[script]</div>.
    As a body-level flex item that wrapper would eat the whole viewport, so
    strip it: the target div goes inside its layout container while the
    scripts stay at end of body (they run after the divs exist in the DOM)."""
    inner = graph_html.strip()
    prefix = '<div style="height:100%; width:100%;">'
    if inner.startswith(prefix):
        inner = inner[len(prefix):]
        assert inner.rstrip().endswith("</div>"), "plotly wrapper has no closing tag"
        inner = inner.rstrip()[: -len("</div>")]
    m = re.search(r'<div id="%s".*?</div>' % div_id, inner, re.S)
    if not m:
        sys.exit(f"plotly output missing <div id={div_id!r}>")
    div = m.group(0)
    return div, inner.replace(div, "", 1)


def build_html_page(graph3d, graph2d, names, clusters, colors, apps, cats,
                    coord5, clu, pnum, app, appmask, uprice, cat, stock, radius, n_neighbors,
                    subtitle, abouttech, opp_json=None):
    div3d, js3d = split_graph_div(graph3d, "map3d")
    div2d, js2d = split_graph_div(graph2d, "map2d")
    return (PAGE_TEMPLATE
            .replace("__DIV3D__", div3d)
            .replace("__DIV2D__", div2d)
            .replace("__JS3D__", js3d)
            .replace("__JS2D__", js2d)
            .replace("__SUBTITLE__", subtitle)
            .replace("__ABOUTTECH__", abouttech)
            .replace("__NAMES__", json.dumps(names, ensure_ascii=False))
            .replace("__CLUSTERS__", json.dumps(clusters, ensure_ascii=False))
            .replace("__COLORS__", json.dumps(colors))
            .replace("__APPS__", json.dumps(apps, ensure_ascii=False))
            .replace("__CATS__", json.dumps(cats, ensure_ascii=False))
            .replace("__R__", repr(radius))
            .replace("__NB__", repr(n_neighbors))
            .replace("__C5__", b64(coord5.astype("<f4")))
            .replace("__CLU__", b64(clu.astype("<u2")))
            .replace("__PNUM__", b64(pnum.astype("<f4")))
            .replace("__APP__", b64(app.astype("u1")))
            .replace("__APPMASK__", b64(appmask.astype("u1")))
            .replace("__UPRICE__", b64(uprice.astype("<f4")))
            .replace("__CAT__", b64(cat.astype("<u2")))
            .replace("__STOCK__", b64(stock.astype("i1")))
            .replace("__OPP_JSON__", json.dumps(opp_json if opp_json is not None else {"note":"Pass --opp-json with opportunity records to activate Gap/Opportunity modes"}, ensure_ascii=False))
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="nemotron",
                    help="nemotron | gemma | exact model string in the table")
    ap.add_argument("--k", type=int, default=24, help="k-means cluster count")
    ap.add_argument("--out", default=None)
    ap.add_argument("--db", default=DB)
    ap.add_argument("--projection", default="pca", choices=["pca", "umap"],
                    help="pca = fast dep-light path (default); umap = "
                         "PCA-50 then UMAP (needs umap-learn)")
    ap.add_argument("--seed", type=int, default=0, help="random seed")
    ap.add_argument("--sample", type=int, default=None,
                    help="use only N random names (fast UI/design testing)")
    ap.add_argument("--neighbors", type=int, default=20,
                    help="similar-products list size in the inspector")
    ap.add_argument("--opp-json", default=None,
                    help="optional opportunity JSON for Gap/Opportunity modes")
    args = ap.parse_args()

    model = MODEL_ALIASES.get(args.model, args.model)
    # per-model output file so renders from different vectors coexist
    if args.out is None:
        tag = args.model if args.model in MODEL_ALIASES else model.replace("/", "-")
        args.out = os.path.join(os.path.dirname(__file__), "..", "exports",
                                "embedding_map.html" if model == DEFAULT_MODEL
                                else f"embedding_map_{tag}.html")

    print(f"loading {model} ...")
    names, mat = load_vectors(args.db, model)
    if args.sample and args.sample < len(names):
        rng = np.random.default_rng(args.seed)
        sel = rng.choice(len(names), args.sample, replace=False)
        names = [names[i] for i in sel]
        mat = mat[sel]
        print(f"sampled {args.sample} names (seed {args.seed})")
    n, d = mat.shape
    print(f"loaded {n} vectors x {d} dims")

    mat = normalize_vectors(mat)
    low = reduce_mid(mat, args.seed)

    labels, centers = cluster_embeddings(low, args.k, args.seed)
    sizes = np.bincount(labels, minlength=args.k)
    print("cluster sizes:", sizes.tolist())

    xyz = np.asarray(project_space(low, 3, args.projection, args.seed),
                     dtype=np.float64)
    xy2 = np.asarray(project_space(low, 2, args.projection, args.seed),
                     dtype=np.float64)

    clusters = label_clusters(names, low, labels, centers, args.k)
    for cl in clusters:
        c = cl["id"]
        cl["cx"], cl["cy"], cl["cz"] = [float(v) for v in xyz[labels == c].mean(axis=0)] \
            if cl["count"] else [0.0, 0.0, 0.0]
        cl["cx2"], cl["cy2"] = [float(v) for v in xy2[labels == c].mean(axis=0)] \
            if cl["count"] else [0.0, 0.0]

    meta, apps_by_name = load_product_meta(args.db, names)
    apps = sorted({m[1] for m in meta.values() if m[1]} | {a for s in apps_by_name.values() for a in s})
    app_idx = {a: i for i, a in enumerate(apps)}
    cat_idx = {c: i for i, c in enumerate(ALL_CATEGORIES)}

    coord5 = np.column_stack([xyz, xy2]).astype(np.float32)
    clu = labels.astype(np.uint16)
    pnum = np.array([meta[nm][0] for nm in names], dtype=np.float32)
    app = np.array([app_idx.get(meta[nm][1], 255)
                    if meta[nm][1] else 255 for nm in names], dtype=np.uint8)
    cat = np.array([cat_idx.get(meta[nm][2], len(ALL_CATEGORIES) - 1)
                    for nm in names], dtype=np.uint16)
    stock = np.array([meta[nm][3] for nm in names], dtype=np.int8)
    appmask = np.zeros(len(names), dtype=np.uint8)
    for idx, nm in enumerate(names):
        m = 0
        for a in apps_by_name.get(nm, ()):
            if a in app_idx:
                m |= (1 << app_idx[a])
        appmask[idx] = m
    uprice = np.full(len(names), np.nan, dtype=np.float32)
    for idx, nm in enumerate(names):
        price = meta[nm][0]
        if not (price is not None and price >= 0):
            continue
        try:
            pf = parse_name(nm, use_llm=False)
            up = unit_price(float(price), pf.get("pack_value"), pf.get("pack_unit"))
        except Exception:
            up = None  # unparseable pack -> stays NaN/unknown
        if up is not None:
            uprice[idx] = float(up)

    colors = golden_palette(args.k)
    hulls = cluster_hulls(xy2, labels, args.k, seed=args.seed)
    print(f"hull territories: {len(hulls)}/{args.k} clusters")

    # neighborhood radius: 5% of the point-cloud diagonal — shared by the
    # inspector's similar-products list on every selection
    radius = 0.05 * float(np.linalg.norm(xyz.max(0) - xyz.min(0)))

    names_arr = np.array(names)
    graph3d, graph2d, _ = build_plotly_scene(
        names_arr, xyz, xy2, labels, clusters, colors, hulls, radius)

    model_short = args.model if args.model in MODEL_ALIASES else model
    subtitle = (f"{n:,} products · Semantic map — "
                "nearby products are more similar")
    abouttech = (f"Model {model}, {args.k} k-means clusters on PCA-50 of "
                 f"cosine-normalized vectors; projection {args.projection}. "
                 "Prices are cheapest-latest offers; stock is the latest "
                 "sighting rollup (unknown where never probed).")
    opp_json = None
    if args.opp_json:
        with open(args.opp_json, "r", encoding="utf-8") as f:
            opp_json = normalize_opportunities(json.load(f), names)
    page = build_html_page(graph3d, graph2d, names, clusters, colors, apps,
                           ALL_CATEGORIES, coord5, clu, pnum, app, appmask, uprice, cat, stock,
                           radius, args.neighbors, subtitle, abouttech,
                           opp_json=opp_json)
    _ = model_short
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"wrote {os.path.abspath(args.out)} "
          f"({os.path.getsize(args.out)/1e6:.1f} MB)")


if __name__ == "__main__":
    main()
