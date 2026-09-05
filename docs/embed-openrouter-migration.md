# Semantic matching — Gemini → NVIDIA (OpenRouter was an intermediate) migration plan

**Status date: 09-05.** Working notes for the embedding-provider switch: what
was measured, what is already shipped, what is pending, and exactly how to run
each pending step. Read together with `src/embed.py` (module docstring) and
the `--embed-catalog` entry in AGENTS.md "Commands".

## Why we switched (the short version)

Semantic matching ("diet coke" → "Coca-Cola Zero Sugar 750ml") was built on
Gemini's free `gemini-embedding-001`. The blocker was **quota semantics**:

| | Gemini free tier (per key) | OpenRouter free tier |
|---|---|---|
| unit billed | **each input ITEM = 1 request** (live-verified 09-05: a 100-name batch burns 100 RPM + 100 RPD) | **each HTTP call = 1 request** (a 1000-name array = 1 request) |
| daily cap | 1,000 RPD | 50 free-model requests/day ($10 credits → 1,000/day) |
| per-minute | 100 RPM / 30k TPM | ~20 RPM |
| reset | midnight PT | midnight UTC (05:30 IST) |
| backfill cost | ~44,487 requests ≈ 45 days across 4 keys | **39–45 requests ≈ 14.5 min, $0** |

Live-measured on 09-05: a 1000-name OpenRouter batch took 19.2s, returned
order-validated vectors, usage showed total tokens (single request). The whole
44,487-name corpus = 39 requests in a measurement run (~14.5 min wall).

## Model choice (live-probed, 60 real catalog names + calibration pairs)

Candidates (all $0 input/output on OpenRouter):

| model | dims | "diet coke"→Coke Zero | "cigarette"→Marlboro | verdict |
|---|---|---|---|---|
| **`nvidia/llama-nemotron-embed-vl-1b-v2`** | 2048 | **#1/59**, cos 0.41 (margin +0.15) | **#1/59**, cos 0.286 (margin +0.03) | **CHOSEN** |
| `nvidia/nemotron-3-embed-1b:free` | 2048 | ok | #3/59, margin **−0.108** | rejected |
| `liquid/lfm-2.5-embedding-350m:free` | 1024 | ok | #3/59, margin −0.104 (nicotine gums outranked Marlboro) | rejected |

Only the winner ranks BOTH ground-truth pairs #1 — the two losers would break
the flagship `/watch cigarette` push case outright.

**Cosine landscape of the winner (drives SEM_LO/SEM_HI):**

| relation | cosine |
|---|---|
| same-product phrasing ("diet coke"↔Coke Zero) | 0.41–0.42 |
| genuine brand match ("cigarette"↔Marlboro Advance King Size) | 0.285–0.29 |
| same-domain non-match (Kinley Soda Water vs diet coke) | 0.23 |
| cross-domain floor (Dettol soap vs cigarette) | 0.12 |
| unrelated noise band | 0.07–0.25 |

`min_match_score` (0.5, same as token matching) implies a semantic pass line at
`SEM_LO + 0.5*(SEM_HI−SEM_LO)`. Shipped calibration: **SEM_LO=0.12,
SEM_HI=0.42** → pass line ≈ 0.27, so Marlboro (0.29) passes, Kinley (0.23)
fails, Coke Zero (0.41) clamps to ~0.97. Recalibrate only with a fresh live
probe, never by feel.

## COMPLETED (all verified, commit pending in this working tree)

1. **config.yaml** — `ai:` section now carries an embeddings sub-config
   independent of the chat LLM (chat stays on Gemini):
   `embedding_base_url: https://openrouter.ai/api/v1`,
   `embedding_key_env: OPENROUTER_API_KEY`, `embedding_model:
   nvidia/llama-nemotron-embed-vl-1b-v2`, `embedding_dims: 2048`,
   `embedding_batch: 1000` — all at the END of the section (miniyaml-safe),
   with the full provider-switch rationale as comments.
2. **src/embed.py — provider-agnostic wiring**: `__init__` reads
   `embedding_base_url` (default OpenRouter) and builds the key pool by
   scanning `{embedding_key_env}`, `{embedding_key_env}_2` … `_9` in `.env` +
   environ (same rotation/failover machinery as before, just re-pointed);
   `embedding_batch` respected via `self.batch` (default 1000).
3. **src/embed.py — request body**: `encoding_format: "float"` always sent;
   `dimensions` sent only while un-rejected — a 400 whose body mentions
   "dimensions" drops the field and retries once (covers both Gemini, which
   honors it, and OpenRouter/nemotron, which returns fixed 2048 dims; probe
   result: the OpenRouter dims probe 429'd on quota before validation, so
   support remains unverified — the fallback makes it moot). `urlopen`
   timeout 60s→300s (1000-name batches take ~19s).
4. **SEM_LO/SEM_HI recalibrated** to the winner's measured landscape (0.12 /
   0.42, see table above), comments updated with the live numbers.
5. **Self-test updated to the new landscape** — fake vectors mirror live
   winner-model cosines (same-product 0.41, same-domain non-match 0.25,
   cross-domain 0.12); `_map` boundary + monotonicity asserts re-anchored;
   all 6 checks pass offline (`python3 -m src.embed`).
6. **Wiring check against real config + .env** (no network): base
   openrouter.ai/api/v1, model nemotron-vl-1b-v2:free, dims 2048, batch 1000,
   exactly 1 key picked up, enabled — asserted programmatically, passed.
7. **Docs** — AGENTS.md (repo map `embed.py` entry, "What this repo is" §2,
   `--embed-catalog` command block with both providers' quota reality),
   README.md backfill comment, GLOSSARY.md backfill entry.
8. **`deals.db` schema → composite PK (model, dims, name)** — a one-time
   in-code migration re-keys the old name-PK table in place (ALTER-via-rename).
   The 6,500 banked Gemini@768 rows are PRESERVED untouched; new Nemotron@2048
   rows are written ADDITIVELY alongside them, so both corpora coexist and a
   later Gemini completion enables a match-quality A/B. The query is embedded
   transiently as "query" type and is NEVER persisted (it would poison the
   passage-keyed corpus). See src/embed.py `_migrate()`.

## PENDING (in order) — with runbooks

### P1. Run the full 44,487-name backfill — ~15 min, 45 requests

**Blocker: today's free budget is spent.** The 09-05 measurement/probe session
used exactly the 50 free-model requests (X-RateLimit-Limit: 50; the last two
dimension probes 429'd with "Add 10 credits to unlock 1000 free model requests
per day"). Options, either works:

- **Wait for the daily reset** — midnight UTC = **05:30 IST**. No cost.
- **User adds $10 credits** on openrouter.ai/credits → limit becomes
  1000 requests/day immediately (still $0 per request on `:free` models).

Then:

    python3 run.py --embed-catalog

- ~45 requests × (19s + 2s pacing) ≈ **15–16 min**, resumable: re-run
  continues from cached names (embedded rows are skipped via `_db_known`).
- Verify completion: embed count printed at the end should read
  **44,487 newly embedded of 44,487** (first run) and a re-run should embed 0.
- If the wall is hit mid-run (429s), the run exits CLEANLY (code 0) with
  "re-run after the provider's daily reset to resume from N cached" — by
  design (`MAX_WALL_ROUNDS=5` → `RPDExhausted` → `ensure()` returns, 1h
  cool-down). Just re-run after reset.
- Every attempt is journaled to `logs/embed_backfill.log` (run start,
  per-batch cumulative counts, stop reason + resume hint; per-name vectors
  are never logged — the file stays small). The `embeddings` table is the
  source of truth for which names are DONE; the log is the human-readable
  attempt ledger, so "where did we stop" survives terminal restarts.

DB growth: 44,487 × 2048 × 4 B ≈ **365 MB** (+ ~20 MB dormant Gemini rows) —
fine on current free disk; the config comment and AGENTS.md note the
smaller-dims upgrade path.

### P2. Sanity-check the archive — one request

    python3 run.py --similar "diet coke" --limit 5
    python3 run.py --similar "cigarette" --limit 5

Expected: Coke Zero at/near #1 (mapped ≈0.97), Marlboro on the list (mapped
≈0.55, just over the pass line — same razor-thin-by-design calibration as the
Gemini build), Kinley-type names below it. Uses 1 request for the phrase
itself; cached names cost nothing.

### P3. Verify the live surfaces still blend correctly

    python3 run.py --search "diet coke"
    # and in Telegram: /watch cigarette (already-watched chats need no change)

The blend invariant `score = max(token_score, semantic_score)` is untouched —
only the vector source changed. Watch pushes for an already-watched term need a
new crawl batch to fire; search is instant. Watch pushes embed the crawl's
candidate names via the same `ensure()` path (1 request per batch, cached names
free), so the 50/day budget covers ~50 fresh-name batches/day.

### P4. (optional) probe `dimensions` support after the reset

Only worth it if DB size ever matters (365 MB is fine). With budget available:

    curl -s https://openrouter.ai/api/v1/embeddings \
      -H "Authorization: Bearer $OPENROUTER_API_KEY" \
      -H "Content-Type: application/json" \
      -d '{"model":"nvidia/llama-nemotron-embed-vl-1b-v2","input":["probe"],"dimensions":768,"encoding_format":"float"}'

200 + 768-dim vector → set `embedding_dims: 768` in config.yaml and RE-RUN the
backfill (rows re-key; ~4× smaller DB). 400/unsupported → leave at 2048.
Do NOT trust `_as_vec`'s Matryoshka truncation for this model — it is not a
documented MRL model.

### P5. (optional) second OpenRouter key for rotation

If daily budget becomes binding with live traffic (search + watch batches),
add `OPENROUTER_API_KEY_2` to `.env` — the pool scan picks it up automatically
(`_2`…`_9` slots). Keys from different accounts pool their free 50/day.

## Invariants that held through the switch (do not regress)

- **Chat LLM unchanged**: `ai.base_url` + `AI_API_KEY` still drive
  `src/ai_assist.py` (Gemini). Only embeddings moved.
- **Blend is max(), never a demotion**; endpoint down/disabled ⇒ exact
  historical token-only behavior (cool-down, no exceptions to callers).
- **One model for the whole corpus** — vectors from different models are
  incompatible (measured: cos 0.098 for same text across models). The
  `embeddings` table's model+dims filter makes mixing impossible at read
  time; INSERT OR REPLACE re-keys rows per backfill.
- **No secrets in config.yaml** — the key lives in `.env`
  (`OPENROUTER_API_KEY`), read via the same `_load_env` path as everything
  else.


## ADDENDUM (09-05, FINAL STATE — supersedes OpenRouter-specific notes above)

The backfill never ran on OpenRouter's free tier (both keys hit their 50 req/day
cap the same day). Final provider is **NVIDIA-hosted**:

- `ai.embedding_base_url: https://integrate.api.nvidia.com/v1`
- `ai.embedding_key_env: NVIDIA_Build_API_KEY`
- `ai.embedding_model: nvidia/llama-nemotron-embed-vl-1b-v2` (2048 dims, fixed)
- `ai.embedding_input_type: passage` (corpus); live queries use `query` type

**Asymmetric model.** nemotron is query/passage ASYMMETRIC: every request must
carry `input_type` (scalar: `passage` for corpus, `query` for searches), a
per-item `modality=["text"]*n`, and `truncate:"NONE"`. Same text typed query vs
passage cosine ~0.48, so the two types must never be mixed.

**Additive multi-model table.** `embeddings` is now `(model, dims, name) PRIMARY
KEY`. Gemini's 6,500 @768 rows are preserved; Nemotron @2048 rows are added
alongside — no overwrite, no wipe. Later Gemini completion + a match-quality A/B
comparison become possible (never cross-model cosine — compare at the
blend/recall level).

**Transient queries.** `boost_many`/`most_similar` embed the live query as
`query` type and score against the persisted `passage` corpus, but the query
vector is NEVER written to the table (would corrupt the passage corpus).
`--embed-catalog` backfill embeds corpus names as `passage` and persists them.

**SEM bounds configurable.** `ai.embedding_sem_lo` / `ai.embedding_sem_hi`
override the defaults (0.12 / 0.42) per model; the rescale is `max(0,
min(1,(cos-lo)/(hi-lo)))`. The same model's landscape still applies, so the
existing calibration holds; later Gemini can carry its own bounds.

**Quota reality (NVIDIA).** Trial NIM is RPM-capped (~40 RPM) with no hard daily
request wall observed; batches of 2000 verified OK (~158s). All provider keys
round-robin per batch and retry when limits reset. Backfill is journaled to
`logs/embed_backfill.log`; re-run `python3 run.py --embed-catalog` to resume.
