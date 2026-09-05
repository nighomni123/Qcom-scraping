#!/usr/bin/env python3
"""
Test embeddinggemma-300m LOCALLY (via Ollama) against deals.db.

What it does
------------
1. Preflight  : finds the Ollama binary, starts `ollama serve` if it isn't
                up, and `ollama pull embeddinggemma` if the model is missing.
2. Backfill   : embeds every distinct product name in deals.db into the
                `embeddings` table under model key = ai.embedding_model
                (default `embeddinggemma`), dims = 768. These rows live
                BESIDE the existing NVIDIA rows — the table is keyed by
                (model, dims, name), so nothing is overwritten.
3. Query      : runs semantic archive queries (`--similar`-style) for a
                built-in set of test phrases plus any `--query` args, printing
                the top-k with RAW cosine (the honest number) and the
                rescaled 0..1 score used by the live blend.

Run
---
    python3 scripts/test_embeddinggemma.py                 # backfill all 44k + demo
    python3 scripts/test_embeddinggemma.py --limit 2000    # tiny smoke test
    python3 scripts/test_embeddinggemma.py --query "diet coke" --query "cigarette"
    python3 scripts/test_embeddinggemma.py --no-backfill   # skip backfill, just query

Requires the Ollama binary somewhere this script can find it:
    env OLLAMA_BIN=/path/to/ollama   (else ./.tools/ollama/ollama, else `ollama`)
Model storage + server host can be overridden the same way as embed.py:
    ai.embedding_base_url  (default http://localhost:11434)
    ai.embedding_model     (default embeddinggemma)
"""
from __future__ import annotations

import argparse
import array
import os
import shutil
import sqlite3
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

from src import embed as E  # noqa: E402


# ---------------------------------------------------------------- preflight --

def find_ollama_bin():
    env = os.environ.get("OLLAMA_BIN")
    if env and os.path.exists(env):
        return env
    local = os.path.join(REPO, ".tools", "ollama", "ollama")
    if os.path.exists(local):
        return local
    on_path = shutil.which("ollama")
    if on_path:
        return on_path
    return None


def server_up(base_url):
    import urllib.request
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/api/tags", timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


def start_server(bin_path, env):
    print(f"[ollama] starting server from {bin_path} ...", flush=True)
    p = subprocess.Popen([bin_path, "serve"], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(120):
        if server_up("http://127.0.0.1:11434"):
            return p
        time.sleep(1)
    raise RuntimeError("ollama serve did not come up within 120s")


def ensure_model(bin_path, model, env):
    import urllib.request, json
    # is it already pulled?
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=5) as r:
            tags = json.loads(r.read().decode()).get("models", [])
        if any(m.get("name", "").split(":")[0] == model for m in tags):
            return
    except Exception:
        pass
    print(f"[ollama] pulling model '{model}' (one-time download, ~1GB) ...",
          flush=True)
    subprocess.run([bin_path, "pull", model], check=True, env=env)


# --------------------------------------------------------------- raw cosine --

def query_vector(emb, text):
    raw = emb._post([text], "query")
    return E._as_vec(raw[0], emb.dims)


def raw_topk(db_path, model, dims, qv, qn, limit=10, min_cos=0.0):
    """Stream the (model,dims) rows and return the top-k by raw cosine."""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "SELECT name, vec FROM embeddings WHERE model=? AND dims=?",
            (model, dims))
        out = []
        for nm, blob in cur:
            v = array.array("f")
            v.frombytes(blob)
            if len(v) != len(qv):
                continue
            cos = E._cos(qv, qn, v)
            if cos >= min_cos:
                out.append((cos, nm))
    finally:
        conn.close()
    out.sort(key=lambda t: (-t[0], t[1]))
    return [(nm, cos) for cos, nm in out[:limit]]


# ----------------------------------------------------------------- main -----

DEFAULT_QUERIES = [
    "diet coke",
    "cigarette",
    "full cream milk",
    "wheat flour atta",
    "instant noodles",
    "laundry detergent",
    "dark chocolate",
    "green tea",
    "shampoo for dry hair",
    "petrol lighter",
]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None,
                    help="backfill at most N NEW names this run")
    ap.add_argument("--query", action="append", default=[],
                    help="extra query phrase (repeatable)")
    ap.add_argument("--no-backfill", action="store_true",
                    help="skip embedding; just run queries")
    ap.add_argument("--db", default=os.path.join(REPO, "deals.db"))
    ap.add_argument("--base-url", default="http://localhost:11434")
    ap.add_argument("--model", default="embeddinggemma")
    ap.add_argument("--dims", type=int, default=768)
    ap.add_argument("--models-dir", default=os.path.join(REPO, ".ollama"))
    args = ap.parse_args()

    bin_path = find_ollama_bin()
    if not bin_path:
        print("ERROR: Ollama binary not found. Set OLLAMA_BIN or put it at "
              "./.tools/ollama/ollama", file=sys.stderr)
        sys.exit(2)
    home_dir = os.path.join(REPO, ".ollama_home")
    ollama_env = dict(os.environ)
    ollama_env["OLLAMA_HOST"] = "127.0.0.1:11434"
    ollama_env["OLLAMA_MODELS"] = args.models_dir
    ollama_env["HOME"] = home_dir
    ollama_env["OLLAMA_HOME"] = home_dir
    ollama_env["OLLAMA_NUM_THREADS"] = "4"   # use all logical cores on this Intel Mac
    if not server_up(args.base_url):
        start_server(bin_path, ollama_env)
    ensure_model(bin_path, args.model, ollama_env)

    # Wire a local Ollama embedder (reuses src/embed.py — same additive table).
    cfg = {"ai": {
        "enabled": True,
        "semantic_matching": True,
        "embedding_provider": "ollama",
        "embedding_base_url": args.base_url,
        "embedding_model": args.model,
        "embedding_dims": args.dims,
    }}
    emb = E.Embedder(cfg, args.db)

    if not args.no_backfill:
        t0 = time.time()
        E.backfill_catalog(cfg, args.db, limit=args.limit)
        print(f"[test] backfill took {time.time()-t0:.1f}s", flush=True)

    cached = emb.cached_count()
    print(f"[test] {cached} names embedded for model={emb.model} dims={emb.dims}",
          flush=True)
    if cached == 0:
        print("ERROR: no embeddings cached — backfill failed?", file=sys.stderr)
        sys.exit(1)

    queries = list(DEFAULT_QUERIES) + args.query
    print("\n" + "=" * 72)
    print("SEMANTIC ARCHIVE QUERIES (raw cosine, then rescaled 0..1)")
    print("=" * 72)
    for q in queries:
        qv = query_vector(emb, q)
        if qv is None:
            print(f"\n>> {q!r}: FAILED to embed query")
            continue
        qn = E._norm(qv)
        top = raw_topk(args.db, emb.model, emb.dims, qv, qn, limit=10)
        print(f"\n>> {q!r}")
        for rank, (nm, cos) in enumerate(top, 1):
            rescaled = E._map(cos, emb.sem_lo, emb.sem_hi)
            print(f"   {rank:2d}. {cos:+.3f}  (rescaled {rescaled:.2f})  {nm}")


if __name__ == "__main__":
    main()
