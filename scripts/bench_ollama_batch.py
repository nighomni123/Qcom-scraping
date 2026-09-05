#!/usr/bin/env python3
"""
Benchmark Ollama /api/embed throughput vs batch size for embeddinggemma.

Embeds the SAME fixed pool of product names at each batch size so throughput
is compared on equal work. Samples free RAM on a background thread to catch the
OOM floor, and tolerates the server being killed by a too-large batch.

Usage:
    python3 scripts/bench_ollama_batch.py [--total 2048] [--sizes 64,128,256,512,1024,2048]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import sqlite3

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OLLAMA_BIN = os.path.join(REPO, ".tools", "ollama", "ollama")
BASE = "http://127.0.0.1:11434"
MODEL = "embeddinggemma"

_min_free = 99.0
_lock = threading.Lock()


def _sampler(stop):
    global _min_free
    while not stop.is_set():
        try:
            out = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
            free = spec = 0
            for line in out.splitlines():
                if "Pages free" in line:
                    free = int(line.split(":")[1].strip().replace(".", ""))
                elif "Pages speculative" in line:
                    spec = int(line.split(":")[1].strip().replace(".", ""))
            gb = (free + spec) * 4096 / 1024 / 1024 / 1024
            with _lock:
                if gb < _min_free:
                    _min_free = gb
        except Exception:
            pass
        time.sleep(0.2)


def free_ram_gb():
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
        free = spec = 0
        for line in out.splitlines():
            if "Pages free" in line:
                free = int(line.split(":")[1].strip().replace(".", ""))
            elif "Pages speculative" in line:
                spec = int(line.split(":")[1].strip().replace(".", ""))
        return (free + spec) * 4096 / 1024 / 1024 / 1024
    except Exception:
        return -1.0


def get_names(total):
    """Distinct product names NOT yet embedded as embeddinggemma@768."""
    c = sqlite3.connect(os.path.join(REPO, "deals.db"))
    rows = [r[0] for r in c.execute(
        "SELECT name FROM ("
        "SELECT DISTINCT name FROM catalog_snapshots WHERE TRIM(COALESCE(name,''))<>'' "
        "UNION SELECT DISTINCT name FROM watchlist WHERE TRIM(COALESCE(name,''))<>'' "
        "UNION SELECT DISTINCT name FROM price_obs WHERE TRIM(COALESCE(name,''))<>'') "
        "WHERE name NOT IN (SELECT name FROM embeddings "
        "WHERE model='embeddinggemma' AND dims=768) LIMIT ?", (total,))]
    c.close()
    return rows


def embed(texts):
    body = json.dumps({"model": MODEL, "input": texts}).encode()
    req = urllib.request.Request(
        BASE + "/api/embed", data=body,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode())["embeddings"]


def ensure_server():
    try:
        urllib.request.urlopen(BASE + "/api/tags", timeout=3)
        return
    except Exception:
        pass
    env = dict(os.environ)
    env["OLLAMA_HOST"] = "127.0.0.1:11434"
    env["OLLAMA_MODELS"] = os.path.join(REPO, ".ollama")
    env["HOME"] = os.path.join(REPO, ".ollama_home")
    env["OLLAMA_HOME"] = os.path.join(REPO, ".ollama_home")
    env["OLLAMA_NUM_THREADS"] = "4"
    subprocess.Popen([OLLAMA_BIN, "serve"], env=env,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(120):
        try:
            urllib.request.urlopen(BASE + "/api/tags", timeout=3)
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError("ollama server did not start")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--total", type=int, default=2200)
    ap.add_argument("--sizes", default="64,128,256,512,712,912,1112,1312,1512,1712,1912,2112")
    args = ap.parse_args()
    sizes = [int(x) for x in args.sizes.split(",")]
    names = get_names(args.total)
    print(f"[bench] pool={len(names)} names, sizes={sizes}", flush=True)
    ensure_server()
    embed(names[:64])  # warmup (load model + first forward)
    print("[bench] warmup done", flush=True)

    stop = threading.Event()
    t = threading.Thread(target=_sampler, args=(stop,), daemon=True)
    t.start()
    global _min_free
    _min_free = free_ram_gb()

    for B in sizes:
        if len(names) < B:
            print(f"B={B}: pool smaller, skip", flush=True)
            continue
        free0 = free_ram_gb()
        result = None
        for attempt in (1, 2):
            t0 = time.time()
            try:
                for i in range(0, len(names), B):
                    embed(names[i:i + B])
                result = (time.time() - t0, None)
                break
            except urllib.error.HTTPError as e:
                err = f"HTTP {e.code}: {e.read().decode()[:150]}"
                if attempt == 1:
                    print(f"B={B:5d}  {err} — retrying once", flush=True)
                    time.sleep(2)
                    continue
                result = (None, err)
            except Exception as e:  # URLError/timeout = server dead, not a 400
                err = f"{type(e).__name__}: {str(e)[:120]}"
                if attempt == 1:
                    print(f"B={B:5d}  server died ({err}) — restarting", flush=True)
                    try:
                        ensure_server()
                        embed(names[:16])
                    except Exception:
                        pass
                    continue
                result = (None, err)
        dt, err = result
        if err:
            print(f"B={B:5d}  FAILED twice: {err}", flush=True)
        else:
            free1 = free_ram_gb()
            print(f"B={B:5d}  items={len(names)}  time={dt:7.2f}s  "
                  f"throughput={len(names)/dt:7.1f} items/s  "
                  f"freeRAM {free0:.2f}->{free1:.2f}GB", flush=True)

    stop.set()
    t.join(timeout=1)
    print(f"[bench] min free RAM observed during run: {_min_free:.2f} GB", flush=True)


if __name__ == "__main__":
    main()
