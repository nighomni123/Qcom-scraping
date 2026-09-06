"""
embed.py — semantic product-name matching (OpenAI-compatible embeddings).

Turns each distinct product name into ONE vector via an OpenAI-compatible
/embeddings endpoint (config.yaml -> ai.embedding_base_url + the env var in
ai.embedding_key_env; default NVIDIA-hosted
https://integrate.api.nvidia.com/v1, model nvidia/llama-nemotron-embed-vl-1b-v2,
fixed 2048 dims, key NVIDIA_Build_API_KEY). Vectors persist in an ADDITIVE
`embeddings` table with a composite primary key (model, dims, name): every
provider's corpus is additive, so already-banked Gemini rows
(gemini-embedding-001 @768) stay untouched while Nemotron rows @2048 are
written alongside them — later a Gemini backfill + an A/B comparison at
MATCH-QUALITY level (never cross-model cosine) become possible. Live matching
embeds only what one search needs; `python3 run.py --embed-catalog` backfills
the whole archive (resumable, journaled to logs/embed_backfill.log).

QUOTA REALITY (live-measured 09-05): Gemini free /embeddings counted EACH
INPUT ITEM as its own request (100-name batch = 100 RPM + 100 RPD; 1,000
RPD/key => ~9 days for 44k names). OpenRouter counts a WHOLE BATCH as ONE
request but its free tier is 50 requests/day shared across accounts. NVIDIA's
trial NIM (integrate.api.nvidia.com) is rate-limited (~40 RPM, no hard daily
request wall observed); batches of 2000 verified OK (~158s). All provider keys
are round-robined per batch and retried when their limits reset.
ponytail: 2048 dims ~8KB/name ~= 365MB for 44k names — acceptable on current
free space; probe smaller dims only if DB growth ever matters.

ASYMMETRIC MODEL: nvidia/llama-nemotron-embed-vl-1b-v2 is query/passage
ASYMMETRIC — `input_type` (a SCALAR per request: "query" vs "passage") plus a
per-item `modality=["text"]*n` and `truncate:"NONE"` are REQUIRED. Corpus
names are embedded as "passage"; live search queries are embedded as "query"
and are NEVER persisted (persisting query vectors would poison the
passage-keyed corpus). Typing genuinely moves the vector (same text typed
query vs passage cosine ~0.48), so the two types must never be mixed.

INVARIANTS
  * Embeddings only decide WHICH products count as a match — never prices,
    stock, fees or alerts. A failed/slow/disabled endpoint degrades to the
    historical token-only match_score everywhere (cool-down between
    retries; callers never see exceptions).
  * The blend is max(token_score, semantic_score): semantics can only LIFT a
    candidate, never demote one.
  * Raw cosine is rescaled onto the token-score scale via SEM_LO/SEM_HI
    (config-overridable per model via ai.embedding_sem_lo / embedding_sem_hi).
"""
from __future__ import annotations

import array
import json
import math
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request

DEFAULT_MODEL = "nvidia/llama-nemotron-embed-vl-1b-v2"
DIMS = 2048            # fixed by the model — no `dimensions` param support
BATCH = 1000           # texts per HTTP request — OpenRouter counts a WHOLE
                       # BATCH as ONE request (free tier: 50/day), so pack
                       # big; 39 requests covered the whole 44k-name corpus
BATCH_PAUSE_SEC = 0.5  # gentle pacing between HTTP batches (config-overridable
                        # via ai.embedding_batch_pause; NVIDIA trial NIM is RPM-,
                        # not req-count-, bound, so a short pause is safe)
COOLDOWN_SEC = 300     # after an endpoint failure, stay token-only this long
MAX_WALL_ROUNDS = 5    # consecutive whole-pool 429 sleeps -> raise: the daily
                       # request budget is spent (OpenRouter resets midnight
                       # UTC; Gemini reset midnight PT). An RPM wall clears
                       # after ONE ~26-60s sleep, so only the daily wall ever
                       # reaches this cap.
RPD_COOLDOWN_SEC = 3600 # daily-budget-exhausted stays token-only 1h (not 5min)
                        # so the live path never grinds walls on every search

# Cosine -> [0,1] rescale onto the token-score scale. min_match_score (0.5)
# implies a semantic pass line at SEM_LO + 0.5*(SEM_HI-SEM_LO) ~= 0.27.
# LIVE-VERIFIED 09-05 against nvidia/llama-nemotron-embed-vl-1b-v2
# @2048 on 60 real catalog names: unrelated pairs floor 0.07-0.24
# (cross-domain 0.12, same-domain-non-match ~0.25); query->genuine match
# 0.29-0.42 ("cigarette" vs "Marlboro Advance King Size" = 0.29 — the
# exact /watch case this module exists for; "diet coke" vs "Coca-Cola
# Zero Sugar 750ml" = 0.41). Pass line ~0.27 sits just under the flagship
# match, safely above the same-domain noise floor. Adjust only with a
# fresh live probe, not intuition.
SEM_LO = 0.12
SEM_HI = 0.42

_SQL = """
CREATE TABLE IF NOT EXISTS embeddings (
    model TEXT,
    dims  INTEGER,
    name  TEXT,
    vec   BLOB,
    ts    REAL,
    PRIMARY KEY (model, dims, name)
);
"""


class RPDExhausted(RuntimeError):
    """All keys' daily request budget is spent (free tier). Not an endpoint
    failure: the backfill re-runs cleanly after the provider's reset
    (OpenRouter midnight UTC, Gemini midnight PT)."""


def _map(cos, lo=SEM_LO, hi=SEM_HI):
    """Rescale a cosine onto the token-match scale (0..1). lo/hi are the cosine
    bounds mapped to 0/1 (per-model overridable via ai.embedding_sem_lo/hi)."""
    return max(0.0, min(1.0, (cos - lo) / (hi - lo)))


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _norm(a):
    return math.sqrt(_dot(a, a)) or 1.0


def _cos(a, na, b):
    return max(-1.0, min(1.0, _dot(a, b) / (na * _norm(b))))


def _as_vec(raw, dims):
    """API vector -> array('f') truncated to dims. Matryoshka training makes
    the PREFIX a valid lower-dim embedding, so truncating a 3072-vector to 768
    is sound; a short vector means the endpoint ignored `dimensions` AND
    returned fewer dims than configured — unusable."""
    v = array.array("f", (float(x) for x in raw or []))
    if len(v) < dims:
        return None
    del v[dims:]
    return v


class Embedder:
    """Batched embeddings client + sqlite cache. All network work happens in
    ensure(); matching (boost_many/cos_score) is pure math after that."""

    def __init__(self, cfg, db_path="deals.db"):
        ai = (cfg or {}).get("ai") or {}
        self.enabled = bool(ai.get("enabled")) and bool(ai.get("semantic_matching", True))
        # Provider: "openai" (default — any OpenAI-compatible /embeddings host,
        # currently NVIDIA's integrate.api.nvidia.com) or "ollama" (a LOCAL
        # Ollama server: no API key, its own /api/embed request shape). Adding a
        # provider NEVER disturbs the others — rows are keyed by (model,dims) so
        # Ollama's vectors live beside NVIDIA's in the same additive table.
        self.provider = str(ai.get("embedding_provider") or "openai").strip().lower()
        if self.provider == "ollama":
            self.base = (str(ai.get("embedding_base_url") or "").strip()
                         or "http://localhost:11434").rstrip("/")
            self.model = str(ai.get("embedding_model") or "embeddinggemma").strip()
            _def_dims, _def_batch = 768, 256
            self.num_ctx = int(ai.get("embedding_ollama_num_ctx") or 512)
            self.query_prefix = str(ai.get("embedding_query_prefix") or "").strip()
            self.passage_prefix = str(ai.get("embedding_passage_prefix") or "").strip()
        else:
            self.base = (str(ai.get("embedding_base_url") or "").strip()
                         or "https://integrate.api.nvidia.com/v1").rstrip("/")
            self.model = str(ai.get("embedding_model") or DEFAULT_MODEL).strip()
            _def_dims, _def_batch = DIMS, BATCH
            self.query_prefix = ""
            self.passage_prefix = ""
        try:
            self.dims = int(ai.get("embedding_dims") or _def_dims)
        except (TypeError, ValueError):
            self.dims = _def_dims
        try:
            self.batch = max(1, int(ai.get("embedding_batch") or _def_batch))
        except (TypeError, ValueError):
            self.batch = _def_batch
        try:
            self.sem_lo = float(ai.get("embedding_sem_lo") or SEM_LO)
        except (TypeError, ValueError):
            self.sem_lo = SEM_LO
        try:
            self.sem_hi = float(ai.get("embedding_sem_hi") or SEM_HI)
        except (TypeError, ValueError):
            self.sem_hi = SEM_HI
        self.input_type = str(ai.get("embedding_input_type") or "passage").strip() or "passage"
        try:
            self.batch_pause = float(ai.get("embedding_batch_pause") or BATCH_PAUSE_SEC)
        except (TypeError, ValueError):
            self.batch_pause = BATCH_PAUSE_SEC
        # NVIDIA-only request fields (input_type/modality/truncate). Other
        # OpenAI-compatible endpoints (Gemini's /v1beta/openai/) REJECT unknown
        # fields with a 400, so providers that want plain semantics set
        # embedding_send_extras: false — the body then carries only
        # model/input/encoding_format(/dimensions).
        self.send_extras = bool(ai.get("embedding_send_extras", True))
        self.db_path = db_path
        self.key_tag = "none"   # masked key that served the last batch (logging)
        self._conn = None       # lazy sqlite (created on first ensure)
        self._cache = {}        # name -> array('f')
        self._known = None      # names already in the DB for this model/dims
        self._lock = threading.RLock()   # RLock: boost_many/cos_score hold
        self._down_until = 0.0  #  it while calling ensure() — not re-entrant
        try:
            from .alert import _load_env
            env = _load_env(os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
        except Exception:
            env = {}
        # Quota is PER API KEY: round-robin the pool (EMB_KEY, EMB_KEY_2, …,
        # default names OPENROUTER_API_KEY*) per batch so per-minute limits
        # are spread before they're hit, and a 429 on one key fails over to
        # the next instead of stalling. Scan EVERY slot (never break on a
        # missing one — .env edits land while processes run); dedupe; empty
        # pool = auth-free endpoint (Ollama).
        key_env = str(ai.get("embedding_key_env") or "NVIDIA_Build_API_KEY").strip()
        self.keys = []
        for suffix in [""] + [f"_{i}" for i in range(1, 10)]:
            k = (env.get(f"{key_env}{suffix}")
                 or os.environ.get(f"{key_env}{suffix}") or "").strip()
            if k and k not in self.keys:
                self.keys.append(k)
        if self.provider == "ollama":
            self.keys = []   # local server — auth-free, no API key

    @property
    def available(self):
        return self.enabled and time.time() >= self._down_until

    # -- network ----------------------------------------------------------

    def _post(self, texts, input_type="passage"):
        """Dispatch to the provider transport. Both return raw vectors in the
        SAME ORDER as `texts` (a list parallel to it)."""
        if self.provider == "ollama":
            return self._post_ollama(texts, input_type)
        return self._post_openai(texts, input_type)

    def _post_ollama(self, texts, input_type="passage"):
        """Local Ollama /api/embed (no API key). Optional query/passage prefixes
        tune the asymmetric embedding — embeddinggemma ignores input_type but
        responds to an instruction prefix. Returns ONE vector per input, or None
        for an input Ollama refused (see _embed_batch)."""
        prefix = self.query_prefix if input_type == "query" else self.passage_prefix
        if prefix:
            texts = [prefix + t for t in texts]
        return self._embed_batch(texts)

    def _ollama_body(self, texts):
        body = {"model": self.model, "input": list(texts)}
        if self.dims and self.dims != 768:   # 768 is embeddinggemma's native dim
            body["dimensions"] = self.dims
        return body

    def _ollama_call(self, body):
        req = urllib.request.Request(
            f"{self.base}/api/embed",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read().decode())

    def _embed_batch(self, texts):
        """Embed a batch; returns a list parallel to `texts` with None for any
        input Ollama refused. Resilient to a 400 from a SINGLE bad name in an
        otherwise-fine batch: drops `dimensions` if that's the complaint, then
        binary-searches to isolate and skip the offending input(s) instead of
        aborting the whole backfill. ponytail: a handful of un-embeddable names
        out of 44k is noise — skip, don't crash."""
        body = self._ollama_body(texts)
        try:
            d = self._ollama_call(body)
        except urllib.error.HTTPError as ex:
            if ex.code != 400:
                raise
            msg = (ex.read().decode()[:400] or "").lower()
            if "dimensions" in msg and "dimensions" in body:
                body.pop("dimensions", None)
                try:
                    d = self._ollama_call(body)
                except urllib.error.HTTPError as ex2:
                    if ex2.code != 400:
                        raise
                    if len(texts) == 1:
                        print(f"[embed] skipping un-embeddable name: {texts[0]!r}",
                              flush=True)
                        return [None]
                    return self._embed_batch(texts[:len(texts) // 2]) + \
                        self._embed_batch(texts[len(texts) // 2:])
            if len(texts) == 1:
                print(f"[embed] skipping un-embeddable name: {texts[0]!r}", flush=True)
                return [None]
            return self._embed_batch(texts[:len(texts) // 2]) + \
                self._embed_batch(texts[len(texts) // 2:])
        vecs = d.get("embeddings")
        if not isinstance(vecs, list) or len(vecs) != len(texts):
            raise RuntimeError(
                f"ollama /api/embed returned "
                f"{len(vecs) if isinstance(vecs, list) else 0}/{len(texts)} "
                f"vectors (model={self.model})")
        return vecs

    def _post_openai(self, texts, input_type="passage"):
        """One POST /embeddings with up to self.batch texts. Returns raw
        vectors in input order. Key handling (quota is PER KEY — distinct
        accounts have separate buckets):
          * every SUCCESSFUL batch rotates to the next key (round-robin),
          * a 429 fails over to the next key immediately (no sleep),
          * only when EVERY key was throttled within this call does it sleep
            (Retry-After or 60s) and retry the round — max MAX_WALL_ROUNDS
            consecutive whole-pool walls, then raise RPDExhausted (an RPM
            wall clears after ONE sleep; only the spent daily budget
            persists). Callers: ensure() degrades to token-only; the
            backfill prints the re-run hint."""
        body = {"model": self.model, "input": list(texts),
                "encoding_format": "float"}
        if self.send_extras:
            body.update({"input_type": input_type,
                         "modality": ["text"] * len(texts),
                         "truncate": "NONE"})
        if self.dims:
            body["dimensions"] = self.dims
        rounds, throttled, no_dims = 0, set(), False
        while True:
            key = self.keys[0]
            req = urllib.request.Request(
                f"{self.base}/embeddings",
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"})
            if key:
                req.add_header("Authorization", "Bearer " + key)
            try:
                with urllib.request.urlopen(req, timeout=300) as r:
                    d = json.loads(r.read().decode())
                if len(self.keys) > 1:
                    self.keys.append(self.keys.pop(0))   # round-robin
                # Masked id of the key that served THIS batch (rotation is
                # observable in the backfill journal: each turn = next key).
                self.key_tag = ("…" + key[-4:]) if key else "local"
                return self._parse_embeddings(d, texts)
            except urllib.error.HTTPError as ex:
                if ex.code == 400 and not no_dims and "dimensions" in \
                        (ex.read().decode()[:400] or "").lower():
                    # endpoint rejected `dimensions` (OpenRouter/nemotron
                    # ignores/forbids it): retry without it — _as_vec still
                    # validates the returned length
                    no_dims = True
                    del body["dimensions"]
                    continue
                if ex.code != 429:
                    raise
                try:
                    wait = max(float(ex.headers.get("Retry-After") or 60), 1.0)
                except (TypeError, ValueError):
                    wait = 60.0
                throttled.add(key)
                if len(self.keys) > 1:
                    self.keys.append(self.keys.pop(0))  # next key, no sleep
                if len(throttled) >= len(self.keys):    # whole pool down
                    rounds += 1
                    if rounds >= MAX_WALL_ROUNDS:
                        raise RPDExhausted(
                            f"all {len(self.keys)} keys throttled for "
                            f"{rounds} consecutive rounds — daily request "
                            f"budget spent (resets at the provider's daily "
                            f"reset: Gemini midnight PT, OpenRouter "
                            f"midnight UTC)")
                    throttled = set()
                    print(f"[embed] quota wall: all {len(self.keys)} keys throttled — sleeping {wait:.0f}s (round {rounds}/{MAX_WALL_ROUNDS})", flush=True)
                    time.sleep(wait)

    def _parse_embeddings(self, d, texts):
        data = sorted((d.get("data") or []),
                       key=lambda e: int(e.get("index", 0)))
        vecs = [e.get("embedding") for e in data]
        if len(vecs) != len(texts) or any(v is None for v in vecs):
            raise RuntimeError(f"endpoint returned {len(vecs)}/{len(texts)} vectors")
        return vecs

    def _log(self, msg):
        """Append one line to logs/embed_backfill.log next to the DB (dir
        created on demand). Best-effort: a logging failure never breaks
        embedding. This file + the embeddings table ARE the resume ledger:
        the table says which names are done, the log says what was tried."""
        try:
            d = os.path.join(os.path.dirname(os.path.abspath(self.db_path)) or ".", "logs")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "embed_backfill.log"), "a") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
        except OSError:
            pass

    def ensure(self, names, progress=False):
        """Embed + persist any uncached names (batched). Returns how many were
        newly embedded; 0 when unavailable or nothing to do. Resumable: names
        already in the DB are skipped, so a failed backfill re-runs cleanly."""
        with self._lock:
            if not self.available:
                return 0
            known = self._db_known()
            todo, seen = [], set()
            for n in names or []:
                n = (n or "").strip()
                if n and n not in seen and n not in self._cache and n not in known:
                    seen.add(n)
                    todo.append(n)
            if not todo:
                return 0
            done, total = 0, len(todo)
            if progress:
                self._log(f"start: {total} names to embed "
                          f"(model={self.model} dims={self.dims} batch={self.batch})")
            for i in range(0, total, self.batch):
                chunk = todo[i:i + self.batch]
                try:
                    vecs = self._post(chunk, self.input_type)
                except RPDExhausted as ex:
                    self._down_until = time.time() + RPD_COOLDOWN_SEC
                    if progress:
                        self._log(f"stopped at {done}/{total}: daily budget spent "
                                  f"— resume: re-run `python3 run.py --embed-catalog`")
                        print(f"[embed] {done}/{total} done, then daily budget "
                              f"spent: {ex} — re-run after the provider's daily "
                              f"reset to resume from {self.cached_count()} cached",
                              flush=True)
                    return done
                except Exception as ex:
                    self._down_until = time.time() + COOLDOWN_SEC
                    if progress:
                        self._log(f"stopped at {done}/{total}: endpoint failed "
                                  f"({str(ex)[:140]}) — resume: re-run the same command")
                        print(f"[embed] endpoint failed after {done}/{total}: "
                              f"{str(ex)[:140]} — re-run the same command to resume",
                              flush=True)
                    return done
                rows = []
                for nm, raw in zip(chunk, vecs):
                    v = _as_vec(raw, self.dims)
                    if v:
                        self._cache[nm] = v
                        rows.append((nm, v.tobytes(), self.dims, self.model, time.time()))
                if rows:
                    self._conn.executemany(
                        "INSERT OR REPLACE INTO embeddings(name,vec,dims,model,ts) "
                        "VALUES(?,?,?,?,?)", rows)
                    self._conn.commit()
                    known.update(r[0] for r in rows)
                done += len(chunk)
                skip = len(chunk) - len(rows)
                if progress:
                    print(f"[embed] {done}/{total} names (key {self.key_tag})", flush=True)
                    self._log(f"batch ok: {done}/{total} names (cached "
                              f"{self.cached_count()}, key {self.key_tag})"
                              + (f" — {skip} names returned unusable vectors, "
                                 f"NOT cached, retry next run" if skip else ""))
                time.sleep(self.batch_pause)
            return done

    # -- sqlite cache -----------------------------------------------------

    def _migrate(self):
        """Upgrade a pre-composite-PK `embeddings` table (name PRIMARY KEY) to
        the additive composite (model, dims, name) PK in place. Idempotent: a
        table that already has the composite PK is left alone. Gemini rows
        (gemini-embedding-001 @768) already banked are preserved unchanged;
        new provider rows are simply added alongside them. ponytail: a one-shot
        ALTER-via-rename keeps the diff tiny and needs no external migration
        script."""
        try:
            row = self._conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='embeddings'").fetchone()
        except sqlite3.Error:
            return
        if not row or row[0] is None or "PRIMARY KEY (model" in row[0].upper():
            return
        self._conn.execute("DROP TABLE IF EXISTS embeddings_old")
        self._conn.execute("ALTER TABLE embeddings RENAME TO embeddings_old")
        self._conn.execute(_SQL)
        self._conn.execute(
            "INSERT OR REPLACE INTO embeddings(model,dims,name,vec,ts) "
            "SELECT model,dims,name,vec,ts FROM embeddings_old")
        self._conn.execute("DROP TABLE embeddings_old")
        self._conn.commit()

    def _db_known(self):
        """(under _lock) Connect + migrate + create table + load the known-name
        set. ponytail: whole-table name set in RAM (~4MB at 42k names) — an
        incremental EXISTS probe if the archive ever grows 10x."""
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._migrate()
            self._conn.execute(_SQL)
            self._conn.commit()
        if self._known is None:
            self._known = {r[0] for r in self._conn.execute(
                "SELECT name FROM embeddings WHERE model=? AND dims=?",
                (self.model, self.dims))}
        return self._known

    def _hydrate(self, names):
        """(under _lock) Load vectors for DB-known names into the process
        cache (chunked IN-select; sqlite caps one statement at ~999 params)."""
        want = [n for n in names if n in self._known and n not in self._cache]
        for i in range(0, len(want), 900):
            chunk = want[i:i + 900]
            q = ",".join("?" * len(chunk))
            for nm, blob in self._conn.execute(
                    f"SELECT name, vec FROM embeddings WHERE model=? AND dims=? "
                    f"AND name IN ({q})", [self.model, self.dims] + chunk):
                v = array.array("f")
                v.frombytes(blob)
                self._cache[nm] = v

    def cached_count(self):
        with self._lock:
            if self._conn is None:
                self._db_known()   # open + migrate so a fresh instance can report
            return self._conn.execute(
                "SELECT COUNT(*) FROM embeddings WHERE model=? AND dims=?",
                (self.model, self.dims)).fetchone()[0]

    # -- matching (pure math after ensure) ---------------------------------

    def boost_many(self, query, names):
        """{name: 0..1 semantic score} for every name with a vector, {} when
        embeddings are unavailable. Scores share the token-score scale, so
        callers blend with max(token, semantic)."""
        query = (query or "").strip()
        if not self.available or not query:
            return {}
        uniq, seen = [], set()
        for n in names or []:
            n = (n or "").strip()
            if n and n not in seen:
                seen.add(n)
                uniq.append(n)
        if not uniq:
            return {}
        with self._lock:
            if not self.available:
                return {}
            # Candidates are passages already in the corpus (backfilled); ensure
            # any missing ones as passages so the live path also grows the
            # corpus. The QUERY is embedded transiently as "query" type and is
            # NEVER persisted (persisting it would poison the passage corpus).
            self.ensure(uniq)
            if not self.available:
                return {}   # ensure() may have tripped the daily cool-down
            try:
                raw = self._post([query], "query")
            except RPDExhausted:
                self._down_until = time.time() + RPD_COOLDOWN_SEC
                return {}
            except Exception:
                self._down_until = time.time() + COOLDOWN_SEC
                return {}
            qv = _as_vec(raw[0], self.dims)
            if not qv:
                return {}
            self._hydrate(uniq)
        qn = _norm(qv)
        return {nm: _map(_cos(qv, qn, v), self.sem_lo, self.sem_hi)
                for nm in uniq if (v := self._cache.get(nm))}

    def most_similar(self, query, limit=10, min_score=0.5):
        """Archive query: names most similar to `query` across the whole
        embeddings table. Streams via its own connection so a 42k-row scan
        costs one pass, not RAM, and never blocks live matching."""
        query = (query or "").strip()
        if not self.available or not query:
            return []
        with self._lock:
            # The query is embedded transiently as "query" type and never
            # persisted; it must not enter the passage-keyed corpus.
            try:
                raw = self._post([query], "query")
            except RPDExhausted:
                self._down_until = time.time() + RPD_COOLDOWN_SEC
                return []
            except Exception:
                self._down_until = time.time() + COOLDOWN_SEC
                return []
            qv = _as_vec(raw[0], self.dims)
            if not qv:
                return []
        qn = _norm(qv)
        conn = sqlite3.connect(self.db_path)
        try:
            cur = conn.execute(
                "SELECT name, vec FROM embeddings WHERE model=? AND dims=?",
                (self.model, self.dims))
            out = []
            for nm, blob in cur:
                if nm == query:
                    continue        # the phrase itself is not an answer
                v = array.array("f")
                v.frombytes(blob)
                s = _map(_cos(qv, qn, v), self.sem_lo, self.sem_hi)
                if s >= min_score:
                    out.append((s, nm))
        finally:
            conn.close()
        out.sort(key=lambda t: (-t[0], t[1]))
        return [(nm, s) for s, nm in out[:limit]]


# --- Offline, model-free expansion across every vector silo -----------------
# The semantic cache (embeddings) is keyed by (model, dims, name). A product's
# vector already in the DB can be reused to find its neighbours WITHOUT calling
# the embedding model -- comparing two stored vectors is pure cosine. This powers
# a lexical-bootstrap -> click -> expand search UX that needs no model access.
# ponytail: reuses _cos/_norm/_map/_as_vec + array; no Embedder, no network.

# Canonical silos for the dashboard's 3-panel view. (model, dims, label)
SEM_SILOS = [
    ("nvidia/llama-nemotron-embed-vl-1b-v2", 2048, "nematron"),
    ("embeddinggemma", 768, "gemma"),
    ("gemini-embedding-001", 768, "google"),
]


def _expand_silo(conn, model, dims, seed_vec, seed_name, limit, min_score):
    out = []
    qn = _norm(seed_vec)
    cur = conn.execute(
        "SELECT name, vec FROM embeddings WHERE model=? AND dims=?",
        (model, dims))
    for nm, blob in cur:
        if nm == seed_name:
            continue
        v = array.array("f")
        v.frombytes(blob)
        s = _map(_cos(seed_vec, qn, v), SEM_LO, SEM_HI)
        if s >= min_score:
            out.append((s, nm))
    out.sort(key=lambda t: (-t[0], t[1]))
    return [(nm, s) for s, nm in out[:limit]]


def similar_across_silos(db_path, name, limit=12, min_score=0.5):
    """Offline expansion: for a seed product *name* already in embeddings, return
    its nearest neighbours from every silo that contains it. Returns
    {label: [(name, score), ...]}; a silo maps to [] when the seed was never
    vectorised by that provider (e.g. Gemini's partial backfill)."""
    name = (name or "").strip()
    if not name:
        return {lab: [] for _, _, lab in SEM_SILOS}
    conn = sqlite3.connect(db_path)
    try:
        res = {}
        for model, dims, lab in SEM_SILOS:
            row = conn.execute(
                "SELECT vec FROM embeddings WHERE model=? AND dims=? AND name=?",
                (model, dims, name)).fetchone()
            if not row:
                res[lab] = []
                continue
            seed = array.array("f")
            seed.frombytes(row[0])
            res[lab] = _expand_silo(conn, model, dims, seed, name, limit, min_score)
    finally:
        conn.close()
    return res


def lexical_seeds(db_path, query, limit=30):
    """Offline product-name finder: distinct catalogue names matching the free
    query (case-insensitive token/substring), best first. The model-free
    bootstrap that lands a user on a real product whose vector we already
    have, so the click can expand it semantically."""
    query = (query or "").strip()
    if not query:
        return []
    toks = [t for t in query.lower().split() if t]
    if not toks:
        return []
    conn = sqlite3.connect(db_path)
    try:
        names = [r[0] for r in conn.execute(
            "SELECT DISTINCT name FROM catalog_snapshots WHERE TRIM(name)<>'' "
            "UNION SELECT DISTINCT name FROM watchlist WHERE TRIM(name)<>'' "
            "UNION SELECT DISTINCT name FROM price_obs WHERE TRIM(name)<>'' ")]
    finally:
        conn.close()
    scored = []
    for nm in names:
        low = (nm or "").lower()
        if not low:
            continue
        if low == query.lower():
            scored.append((0, len(toks), low, nm))   # exact name wins
            continue
        hit = sum(1 for t in toks if t in low)
        if hit:
            scored.append((1, hit, low, nm))
    scored.sort(key=lambda x: (x[0], -x[1], x[2]))
    return [nm for *_, nm in scored[:limit]]


_EMB = {}
_EMB_LOCK = threading.Lock()


def get_embedder(cfg, db_path="deals.db"):
    """Process-wide Embedder per db_path (mirrors tgbot.get_watcher), so the
    bot, monitor batches and CLI searches share one cache + one cool-down."""
    with _EMB_LOCK:
        if db_path not in _EMB:
            _EMB[db_path] = Embedder(cfg, db_path)
        return _EMB[db_path]


def backfill_catalog(cfg, db_path, limit=None):
    """Vectorize every distinct product name across catalog_snapshots,
    watchlist and price_obs. Resumable; prints per-batch progress."""
    emb = get_embedder(cfg, db_path)
    conn = sqlite3.connect(db_path)
    try:
        # Strip + dedupe: ensure() embeds the STRIPPED name, so the pending/
        # limit accounting below must compare the same normalization (a raw
        # name with trailing whitespace would read as phantom "pending" even
        # once its stripped twin is cached).
        names = list(dict.fromkeys(r[0].strip() for r in conn.execute(
            "SELECT DISTINCT name FROM catalog_snapshots WHERE TRIM(COALESCE(name,''))<>'' "
            "UNION SELECT DISTINCT name FROM watchlist WHERE TRIM(COALESCE(name,''))<>'' "
            "UNION SELECT DISTINCT name FROM price_obs WHERE TRIM(COALESCE(name,''))<>''")))
    finally:
        conn.close()
    if limit:
        known = emb._db_known()          # limit counts NEW names this run,
        names = [n for n in names if n not in known][:limit]  # not re-embeds
    n = emb.ensure(names, progress=True)
    known = emb._db_known()
    pending = sum(1 for x in names if x not in known)
    emb._log(f"run done: +{n} this run; {len(known)} names cached "
             f"({emb.model}@{emb.dims}); {pending} still pending of the "
             f"{len(names)} names in this run's list")
    print(f"[embed-catalog] {n} newly embedded of {len(names)} distinct names "
          f"({emb.cached_count()} total cached, {pending} pending)")


if __name__ == "__main__":
    # Offline self-test: geometry, rescale bounds, end-to-end boost with a
    # FAKE transport (no network), the disabled path, and DB persistence
    # (a second Embedder must answer from sqlite without any network).
    import shutil
    import tempfile

    assert _as_vec([1.0] * 10, 4) == array.array("f", [1.0] * 4), "Matryoshka truncation"
    assert _as_vec([1.0] * 3, 4) is None, "short vector must be unusable"
    assert abs(_cos(array.array("f", [1, 0]), 1.0, array.array("f", [0, 1]))) < 1e-9
    assert abs(_cos(array.array("f", [1, 0]), 1.0, array.array("f", [1, 0])) - 1.0) < 1e-9
    assert _map(0.12) == 0.0 and _map(0.42) == 1.0
    assert _map(0.25) < _map(0.29) < _map(0.41), "rescale must be monotonic"

    tmp = tempfile.mkdtemp(prefix="dsh_embed_")
    cfg = {"ai": {"enabled": True, "semantic_matching": True,
                  "embedding_model": "fake", "embedding_dims": 4}}

    # Fake vectors mirror LIVE nvidia/llama-nemotron-embed-vl-1b-v2
    # cosines (09-05 probe on 60 real catalog names): same-product 0.41,
    # same-domain-non-match 0.25, cross-domain floor 0.12 — so the pass/fail
    # assertions below sit on the real landscape, not invented geometry.
    # q = the query's vector ("diet coke").
    q = [1.0, 0.0, 0.0, 0.0]

    def _at(cos):
        return [cos, math.sqrt(1.0 - cos * cos), 0.0, 0.0]
    fake_vecs = {
        "Coca-Cola Zero Sugar 750ml": _at(0.41),
        "Kinley Soda Water Bottle":   _at(0.25),
        "Dettol Original Soap":       _at(0.12),
    }
    calls = []

    def fake_post(texts, input_type="passage"):
        calls.append(list(texts))
        out = []
        for t in texts:
            out.append(fake_vecs.get(t) if t in fake_vecs else q)
        return out

    emb = Embedder(cfg, os.path.join(tmp, "t.db"))
    emb._post = fake_post
    boosts = emb.boost_many("diet coke", list(fake_vecs))
    assert calls[-1] == ["diet coke"], "query must be embedded as a transient query-type call"
    assert boosts.get("Coca-Cola Zero Sugar 750ml", 0) >= 0.5, \
        "same-product phrasing must clear min_match_score"
    assert boosts.get("Kinley Soda Water Bottle", 1) < 0.5, \
        "category-adjacent must NOT pass (no false watch pushes)"
    assert boosts.get("Dettol Original Soap", 1) < 0.05, "unrelated ~floors at 0"

    # Disabled path: {} and zero network.
    off = Embedder({"ai": {"enabled": False}}, os.path.join(tmp, "t.db"))
    n_calls = len(calls)
    assert off.boost_many("diet coke", ["Dettol Original Soap"]) == {}
    assert len(calls) == n_calls, "disabled embedder must not hit the network"

    # Persistence: a FRESH Embedder answers cached passages from sqlite with
    # NO re-embedding — the resumable-backfill guarantee. The only network call
    # is the transient query (which is never persisted).
    emb2 = Embedder(cfg, os.path.join(tmp, "t.db"))
    posts = []

    def echo_post(texts, input_type="passage"):
        posts.append(list(texts))
        return [q if t == "diet coke" else fake_vecs.get(t, q) for t in texts]
    emb2._post = echo_post
    again = emb2.boost_many("diet coke", list(fake_vecs))
    assert again.get("Coca-Cola Zero Sugar 750ml", 0) >= 0.5
    assert again.get("Dettol Original Soap", 1) < 0.05
    # cached passages come from sqlite; only the query is embedded (transient)
    assert posts == [["diet coke"]], "cached passages must NOT be re-embedded"
    top = emb2.most_similar("diet coke", limit=3, min_score=0.5)
    assert top and top[0][0] == "Coca-Cola Zero Sugar 750ml", "archive query"

    # RPD wall-cap: an always-429 transport must raise RPDExhausted after
    # MAX_WALL_ROUNDS whole-pool rounds (MAX-1 sleeps — an RPM wall clears
    # on the first sleep and never reaches the cap), and ensure() must
    # swallow it into a 1h token-only cool-down, never re-raise.
    emb3 = Embedder(cfg, os.path.join(tmp, "t.db"))
    emb3.keys = ["k1", "k2"]
    slept = []
    real_urlopen, real_sleep = urllib.request.urlopen, time.sleep

    def always_429(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 429, "Too Many Requests",
            {"Retry-After": "60"}, None)
    urllib.request.urlopen, time.sleep = always_429, slept.append
    try:
        try:
            emb3._post(["x", "y"])
            raise AssertionError("RPD wall must raise RPDExhausted")
        except RPDExhausted:
            pass
        assert len(slept) == MAX_WALL_ROUNDS - 1, \
            f"cap must fire after {MAX_WALL_ROUNDS} rounds; slept {len(slept)}"
    finally:
        urllib.request.urlopen, time.sleep = real_urlopen, real_sleep

    def rpd_boom(texts, input_type="passage"):
        raise RPDExhausted("daily quota spent")
    emb3._post = rpd_boom
    assert emb3.ensure(["brand new name"]) == 0, \
        "RPD exhaustion must degrade to 0, not raise"
    assert emb3._down_until >= time.time() + RPD_COOLDOWN_SEC - 1, \
        "RPD cool-down must be 1h"

    # Request-body gate: the default (NVIDIA) embedder sends the asymmetric
    # extras; a Gemini-style embedder (embedding_send_extras: false) must send
    # a plain body — Gemini 400s on unknown fields like input_type.
    captured = {}
    real_urlopen2 = urllib.request.urlopen

    def capture(req, timeout=None):
        captured["body"] = json.loads(req.data.decode())
        raise urllib.error.HTTPError(req.full_url, 429, "x", {"Retry-After": "1"}, None)
    urllib.request.urlopen = capture
    try:
        nvidia = Embedder(cfg, os.path.join(tmp, "n.db"))
        try:
            nvidia._post(["a"], "passage")
        except Exception:
            pass
        assert "input_type" in captured["body"], "NVIDIA body must carry input_type"
        gem = Embedder({"ai": {"enabled": True, "embedding_model": "m",
                               "embedding_send_extras": False}},
                       os.path.join(tmp, "g.db"))
        try:
            gem._post(["a"], "passage")
        except Exception:
            pass
        b = captured["body"]
        assert set(b) == {"model", "input", "encoding_format", "dimensions"}, \
            f"Gemini body must be plain, got {sorted(b)}"
        assert "input_type" not in b and "modality" not in b, \
            "extras must be omitted when embedding_send_extras is false"
    finally:
        urllib.request.urlopen = real_urlopen2

    # Per-batch key rotation: every successful batch moves to the next key in
    # the pool, and the key that served it is exposed (masked) for journaling.
    rot = Embedder(cfg, os.path.join(tmp, "r.db"))
    rot.keys = ["key-aaaa", "key-bbbb", "key-cccc"]
    seen_keys = []

    def rotating_urlopen(req, timeout=None):
        auth = dict(req.header_items()).get("Authorization", "")
        seen_keys.append(auth[-4:])                     # masked key tail
        raise urllib.error.HTTPError(req.full_url, 429, "x", {"Retry-After": "1"}, None)
    real_uo, real_sl = urllib.request.urlopen, time.sleep
    urllib.request.urlopen, time.sleep = rotating_urlopen, slept.append
    try:
        try:
            rot._post(["a"])
        except Exception:
            pass
        # First call rotates aabbcc then sleeps — the pool order must advance
        # one key per attempt (failover AND round-robin share the same motion).
        assert seen_keys[:3] == ["aaaa", "bbbb", "cccc"], seen_keys[:3]
    finally:
        urllib.request.urlopen, time.sleep = real_uo, real_sl

    # Migration: an old name-PK `embeddings` table must upgrade to the
    # composite (model, dims, name) PK without losing rows, and the same name
    # must be allowed under two models (additivity).
    mig = os.path.join(tmp, "mig.db")
    c = sqlite3.connect(mig)
    c.execute("CREATE TABLE embeddings(name TEXT PRIMARY KEY, vec BLOB, "
              "dims INTEGER, model TEXT, ts REAL)")
    c.execute("INSERT INTO embeddings VALUES(?,?,?,?,?)",
              ("Old Product", array.array("f", [1, 2, 3, 4]).tobytes(), 768,
               "gemini-embedding-001", 1.0))
    c.commit(); c.close()
    m = Embedder(cfg, mig)
    m._db_known()
    assert m.cached_count() == 0, "migration keeps row under its own model/dims"
    c = sqlite3.connect(mig)
    total = c.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    c.close()
    assert total == 1, "migration must preserve the old row"
    # composite PK: same name under a different model is a distinct row
    m._conn.execute(
        "INSERT OR REPLACE INTO embeddings(model,dims,name,vec,ts) "
        "VALUES(?,?,?,?,?)",
        ("fake", 4, "Old Product", array.array("f", [1, 2, 3, 4]).tobytes(), 2.0))
    m._conn.commit()
    c = sqlite3.connect(mig)
    n = c.execute("SELECT COUNT(*) FROM embeddings WHERE name=?",
                  ("Old Product",)).fetchone()[0]
    c.close()
    assert n == 2, "composite PK must allow the same name under two models"

    # Additivity: writing model B must not disturb model A's rows.
    add = os.path.join(tmp, "add.db")
    a = Embedder(cfg, add)
    a._post = lambda texts, input_type="passage": [
        [1.0, 0, 0, 0] if t == "A1" else [0, 1.0, 0, 0] for t in texts]
    a.ensure(["A1"])
    cfgB = {"ai": {"enabled": True, "semantic_matching": True,
                   "embedding_model": "other-model", "embedding_dims": 4}}
    b = Embedder(cfgB, add)
    b._post = lambda texts, input_type="passage": [
        [0, 0, 1.0, 0] if t == "B1" else [0, 0, 0, 1.0] for t in texts]
    b.ensure(["B1"])
    a2 = Embedder(cfg, add); a2._db_known()
    b2 = Embedder(cfgB, add); b2._db_known()
    assert a2.cached_count() == 1, "model A rows must survive a model B write"
    assert b2.cached_count() == 1, "model B must write its own row"

    # Typed query: the live query is embedded as input_type=query and is NEVER
    # persisted as a corpus row.
    qdb = os.path.join(tmp, "q.db")
    qe = Embedder(cfg, qdb)
    types = []

    def typed_post(texts, input_type="passage"):
        types.append(input_type)
        return [[1.0, 0, 0, 0] for _ in texts]
    qe._post = typed_post
    qe.boost_many("live query", ["Coca-Cola Zero Sugar 750ml", "Dettol Original Soap"])
    assert "query" in types, "live query must be embedded as input_type=query"
    qc = Embedder(cfg, qdb); qc._db_known()
    assert "live query" not in qc._db_known(), "query vector must not be persisted"

    shutil.rmtree(tmp, ignore_errors=True)
    print("[embed] self-test OK: geometry, rescale, boost, disabled path, "
          "persistence, RPD wall-cap, key rotation (7 checks, 0 network calls "
          "after backfill)")
