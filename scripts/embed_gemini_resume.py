#!/usr/bin/env python3
"""
embed_gemini_resume.py — continue the GEMINI corpus backfill (additive).

The `embeddings` table is keyed (model, dims, name): this fills the GEMINI
corpus (gemini-embedding-001 @768 — 6,500 rows banked 09-05) WITHOUT touching
the NVIDIA @2048 rows that live search uses, and WITHOUT editing config.yaml
(the repo default stays NVIDIA; this is an explicitly-run job).

Provider facts (live-measured 09-05, docs/embed-openrouter-migration.md):
  * Gemini free tier counts EACH INPUT ITEM as 1 request: 100 RPM + 30k TPM,
    1,000 RPD per key. 4 keys (AI_API_KEY.._4) => ~4,000 names/day.
  * The OpenAI-compat endpoint REJECTS unknown fields (input_type/modality/
    truncate are NVIDIA-only) => embedding_send_extras: false.
  * `dimensions: 768` is honored natively — vectors match the banked corpus.
  * Daily reset: midnight PT. Re-run daily until pending hits 0; fully
    resumable (cached names are skipped; the embeddings table + journal at
    logs/embed_backfill.log are the ledger).

Pacing: batch=100 + 16s pause ~= 375 items/min pool-wide (~94 RPM/key, under
the 100 RPM wall) so whole-pool 429 stalls are rare; the wall machinery
(Retry-After sleep, MAX_WALL_ROUNDS, RPDExhausted) absorbs leftovers and stops
the run cleanly once the daily budget is spent — just re-run after the reset.

Usage:
  python3 scripts/embed_gemini_resume.py [--limit N]
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)          # config.yaml + deals.db + logs/ are root-relative

try:
    import yaml
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
except ModuleNotFoundError:
    from src import miniyaml
    cfg = miniyaml.load("config.yaml")

ai = cfg.setdefault("ai", {})
ai.update({
    "embedding_base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
    "embedding_key_env": "AI_API_KEY",       # the original Gemini pool (4 keys)
    "embedding_model": "gemini-embedding-001",
    "embedding_dims": 768,
    "embedding_batch": 100,
    "embedding_send_extras": False,          # Gemini 400s on NVIDIA-only fields
    "embedding_batch_pause": 2,              # batch 2: ~45 names/min, far under RPM
    "semantic_matching": True,
    "enabled": True,
})

limit = None
if "--limit" in sys.argv:
    limit = int(sys.argv[sys.argv.index("--limit") + 1])
if "--batch" in sys.argv:
    # Each batch burns its items on ONE key (every input item = 1 request for
    # Gemini), so batch size = per-key burn granularity: batch 50 + limit 200
    # ~= 50 requests against each of the 4 rotating keys.
    ai["embedding_batch"] = int(sys.argv[sys.argv.index("--batch") + 1])

from src.embed import backfill_catalog
backfill_catalog(cfg, os.path.join(ROOT, "deals.db"), limit=limit)
