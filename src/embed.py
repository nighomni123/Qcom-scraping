"""
embed.py — semantic product-name matching (OpenRouter embeddings route).

Turns each distinct product name into ONE vector via an OpenAI-compatible
/embeddings endpoint (config.yaml -> ai.embedding_base_url + the env var in
ai.embedding_key_env, default OPENROUTER_API_KEY; model
nvidia/llama-nemotron-embed-vl-1b-v2:free, fixed 2048 dims). Vectors persist
in an additive `embeddings` table keyed by name (one vector per distinct
name, shared across platforms and stores; INSERT OR REPLACE means a provider
switch transparently re-keys rows on the next backfill). Live matching embeds
only what one search needs (query + candidates = a single batched request);
`python3 run.py --embed-catalog` backfills the archive (resumable).

QUOTA REALITY (live-measured 09-05): Gemini's free /embeddings counted EACH
INPUT ITEM as its own request (100-name batch = 100 RPM + 100 RPD; 1,000
RPD/key => ~9 days for 44k names). OpenRouter counts a WHOLE BATCH as ONE
request: 1000 names/request finished 44,487 names in 39 requests / 14.5 min.
Free tier = 50 requests/day (X-RateLimit-Limit), $10 credits -> 1000/day.
Model choice live-verified on 60 real catalog names: the only free model
where BOTH ground-truth pairs rank #1 ("diet coke"->Coke Zero +0.15,
"cigarette"->Marlboro +0.03); the other free candidates ranked Marlboro #3.
ponytail: 2048 dims ~8KB/name ~= 365MB for 44k names — if the DB ever
matters, probe `dimensions` support or switch to a smaller model then.

INVARIANTS
  * Embeddings only decide WHICH products count as a match — never prices,
    stock, fees or alerts. A failed/slow/disabled endpoint degrades to the
    historical token-only match_score everywhere (cool-down between
    retries; callers never see exceptions).
  * The blend is max(token_score, semantic_score): semantics can only LIFT a
    candidate, never demote one.
  * Raw cosine is rescaled onto the token-score scale via SEM_LO/SEM_HI — the
    ONE calibration knob (verified against live cosines; see self-test).
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

DEFAULT_MODEL = "nvidia/llama-nemotron-embed-vl-1b-v2:free"
DIMS = 2048            # fixed by the model — no `dimensions` param support
BATCH = 1000           # texts per HTTP request — OpenRouter counts a WHOLE
                       # BATCH as ONE request (free tier: 50/day), so pack
                       # big; 39 requests covered the whole 44k-name corpus
BATCH_PAUSE_SEC = 2.0  # gentle pacing between HTTP batches
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
# LIVE-VERIFIED 09-05 against nvidia/llama-nemotron-embed-vl-1b-v2:free
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
    name  TEXT PRIMARY KEY,
    vec   BLOB,
    dims  INTEGER,
    model TEXT,
    ts    REAL
);
"""


class RPDExhausted(RuntimeError):
    """All keys' daily request budget is spent (free tier). Not an endpoint
    failure: the backfill re-runs cleanly after the provider's reset
    (OpenRouter midnight UTC, Gemini midnight PT)."""


def _map(cos):
    """Rescale a cosine onto the token-match scale (0..1)."""
    return max(0.0, min(1.0, (cos - SEM_LO) / (SEM_HI - SEM_LO)))


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
        # Embeddings get their OWN endpoint + key, independent of chat (ai.base_url
        # stays the chat LLM): a provider switch on one never disturbs the other.
        self.base = (str(ai.get("embedding_base_url") or "").strip()
                     or "https://openrouter.ai/api/v1").rstrip("/")
        self.model = str(ai.get("embedding_model") or DEFAULT_MODEL).strip()
        try:
            self.dims = int(ai.get("embedding_dims") or DIMS)
        except (TypeError, ValueError):
            self.dims = DIMS
        try:
            self.batch = max(1, int(ai.get("embedding_batch") or BATCH))
        except (TypeError, ValueError):
            self.batch = BATCH
        self.db_path = db_path
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
        key_env = str(ai.get("embedding_key_env") or "OPENROUTER_API_KEY").strip()
        self.keys = []
        for suffix in [""] + [f"_{i}" for i in range(1, 10)]:
            k = (env.get(f"{key_env}{suffix}")
                 or os.environ.get(f"{key_env}{suffix}") or "").strip()
            if k and k not in self.keys:
                self.keys.append(k)

    @property
    def available(self):
        return self.enabled and time.time() >= self._down_until

    # -- network ----------------------------------------------------------

    def _post(self, texts):
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
                            f"reset: OpenRouter midnight UTC)")
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
            for i in range(0, total, self.batch):
                chunk = todo[i:i + BATCH]
                try:
                    vecs = self._post(chunk)
                except RPDExhausted as ex:
                    self._down_until = time.time() + RPD_COOLDOWN_SEC
                    if progress:
                        print(f"[embed] {done}/{total} done, then daily budget "
                              f"spent: {ex} — re-run after the provider's daily "
                              f"reset to resume from {self.cached_count()} cached",
                              flush=True)
                    return done
                except Exception as ex:
                    self._down_until = time.time() + COOLDOWN_SEC
                    if progress:
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
                if progress:
                    print(f"[embed] {done}/{total} names", flush=True)
                time.sleep(BATCH_PAUSE_SEC)
            return done

    # -- sqlite cache -----------------------------------------------------

    def _db_known(self):
        """(under _lock) Connect + create table + load the known-name set.
        ponytail: whole-table name set in RAM (~4MB at 42k names) — an
        incremental EXISTS probe if the archive ever grows 10x."""
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
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
                return 0
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
            self.ensure([query] + uniq)
            self._hydrate([query] + uniq)
        qv = self._cache.get(query)
        if not qv:
            return {}
        qn = _norm(qv)
        return {nm: _map(_cos(qv, qn, v))
                for nm in uniq if (v := self._cache.get(nm))}

    def most_similar(self, query, limit=10, min_score=0.5):
        """Archive query: names most similar to `query` across the whole
        embeddings table. Streams via its own connection so a 42k-row scan
        costs one pass, not RAM, and never blocks live matching."""
        query = (query or "").strip()
        if not self.available or not query:
            return []
        with self._lock:
            self.ensure([query])
            self._hydrate([query])   # cached query must load before use
        qv = self._cache.get(query)
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
                s = _map(_cos(qv, qn, v))
                if s >= min_score:
                    out.append((s, nm))
        finally:
            conn.close()
        out.sort(key=lambda t: (-t[0], t[1]))
        return [(nm, s) for s, nm in out[:limit]]


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
        names = [r[0] for r in conn.execute(
            "SELECT DISTINCT name FROM catalog_snapshots WHERE TRIM(COALESCE(name,''))<>'' "
            "UNION SELECT DISTINCT name FROM watchlist WHERE TRIM(COALESCE(name,''))<>'' "
            "UNION SELECT DISTINCT name FROM price_obs WHERE TRIM(COALESCE(name,''))<>''")]
    finally:
        conn.close()
    if limit:
        known = emb._db_known()          # limit counts NEW names this run,
        names = [n for n in names if n not in known][:limit]  # not re-embeds
    n = emb.ensure(names, progress=True)
    print(f"[embed-catalog] {n} newly embedded of {len(names)} distinct names "
          f"({emb.cached_count()} total cached)")


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

    # Fake vectors mirror LIVE nvidia/llama-nemotron-embed-vl-1b-v2:free
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

    def fake_post(texts):
        calls.append(list(texts))
        out = []
        for t in texts:
            out.append(fake_vecs.get(t) if t in fake_vecs else q)
        return out

    emb = Embedder(cfg, os.path.join(tmp, "t.db"))
    emb._post = fake_post
    boosts = emb.boost_many("diet coke", list(fake_vecs))
    assert "diet coke" in calls[0], "query must be embedded with candidates"
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

    # Persistence: a FRESH Embedder on the same DB answers from sqlite with
    # no network at all (the resumable-backfill guarantee).
    emb2 = Embedder(cfg, os.path.join(tmp, "t.db"))

    def boom(texts):
        raise AssertionError("network must not be called for cached names")
    emb2._post = boom
    again = emb2.boost_many("diet coke", list(fake_vecs))
    assert again.get("Coca-Cola Zero Sugar 750ml", 0) >= 0.5
    assert again.get("Dettol Original Soap", 1) < 0.05
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

    def rpd_boom(texts):
        raise RPDExhausted("daily quota spent")
    emb3._post = rpd_boom
    assert emb3.ensure(["brand new name"]) == 0, \
        "RPD exhaustion must degrade to 0, not raise"
    assert emb3._down_until >= time.time() + RPD_COOLDOWN_SEC - 1, \
        "RPD cool-down must be 1h"

    shutil.rmtree(tmp, ignore_errors=True)
    print("[embed] self-test OK: geometry, rescale, boost, disabled path, "
          "persistence, RPD wall-cap (6 checks, 0 network calls after backfill)")
