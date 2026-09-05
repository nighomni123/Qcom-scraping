"""
embed.py — semantic product-name matching (Gemini embeddings route).

Turns each distinct product name into ONE vector via the same OpenAI-compatible
endpoint the AI assistant already uses (config.yaml -> ai.base_url + AI_API_KEY
in .env; model ai.embedding_model, default gemini-embedding-001 — Matryoshka-
trained, so 768 output dims keep near-full quality at ~3KB per name; Google
recommends 3072/1536/768). Vectors persist forever in an additive `embeddings`
table keyed by name (one vector per distinct name, shared across platforms and
stores). Live matching embeds only what one search needs (query + candidates
~= a single batched request); `python3 run.py --embed-catalog` backfills the
archive (~430 batched requests for 42k names, resumable).

INVARIANTS
  * Embeddings only decide WHICH products count as a match — never prices,
    stock, fees or alerts. A failed/slow/disabled endpoint degrades to the
    historical token-only match_score everywhere (5-minute cool-down between
    retries; callers never see exceptions).
  * The blend is max(token_score, semantic_score): semantics can only LIFT a
    candidate, never demote one.
  * Raw cosine is rescaled onto the token-score scale via SEM_LO/SEM_HI — the
    ONE calibration knob (verified against live Gemini cosines; see self-test).
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

DEFAULT_MODEL = "gemini-embedding-001"
DIMS = 768              # Matryoshka output dims (768 keeps rows ~3KB)
BATCH = 100             # texts per API request (free-tier batching)
BATCH_PAUSE_SEC = 0.6   # gentle pacing between backfill batches (~100 req/min ceiling)
COOLDOWN_SEC = 300      # after an endpoint failure, stay token-only this long

# Cosine -> [0,1] rescale onto the token-score scale. min_match_score (0.5)
# implies a semantic pass line of cos ~= 0.61. LIVE-VERIFIED 09-05 against
# gemini-embedding-001 @768 dims: unrelated pairs floor at 0.50-0.55;
# category matches 0.58-0.66 (incl. "cigarette" vs "Marlboro Advance King
# Size" = 0.61 — the exact /watch case this module exists for); same-product
# phrasings 0.65-0.80 ("diet coke" vs "Coca-Cola Zero Sugar 750ml" = 0.67);
# near-exact 0.80-0.86. Adjust only with a fresh live probe, not intuition.
SEM_LO = 0.50
SEM_HI = 0.72

_SQL = """
CREATE TABLE IF NOT EXISTS embeddings (
    name  TEXT PRIMARY KEY,
    vec   BLOB,
    dims  INTEGER,
    model TEXT,
    ts    REAL
);
"""


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
        self.base = (str(ai.get("base_url") or "").strip()
                     or "https://api.openai.com/v1").rstrip("/")
        self.model = str(ai.get("embedding_model") or DEFAULT_MODEL).strip()
        try:
            self.dims = int(ai.get("embedding_dims") or DIMS)
        except (TypeError, ValueError):
            self.dims = DIMS
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
        # Quota is PER API KEY: round-robin the pool (AI_API_KEY, _2, _3, …)
        # per batch so per-minute limits are spread before they're hit, and a
        # 429 on one key fails over to the next instead of stalling. Scan
        # EVERY slot (never break on a missing one — .env edits land while
        # processes run); dedupe; empty pool = auth-free endpoint (Ollama).
        self.keys = []
        for suffix in [""] + [f"_{i}" for i in range(1, 10)]:
            k = (env.get(f"AI_API_KEY{suffix}")
                 or os.environ.get(f"AI_API_KEY{suffix}") or "").strip()
            if k and k not in self.keys:
                self.keys.append(k)
        self.key = self.keys[0] if self.keys else ""

    @property
    def available(self):
        return self.enabled and time.time() >= self._down_until

    # -- network ----------------------------------------------------------

    def _post(self, texts):
        """One POST /embeddings with up to BATCH texts. Returns raw vectors
        in input order. Key handling (quota is PER KEY, so a pool spreads
        per-minute limits):
          * every SUCCESSFUL batch rotates to the next key (round-robin),
          * a 429 fails over to the next key immediately (no sleep),
          * only when EVERY key was throttled within this call does it sleep
            (Retry-After or 60s) and retry the round — max 2 full rounds,
            then raise (caller applies the cool-down).
        Live-observed 09-05: full-throttle backfill trips 429s constantly;
        3 keys + round-robin keep ~3x RPM with no stalls."""
        body = {"model": self.model, "input": list(texts), "dimensions": self.dims}
        rounds, throttled = 0, set()
        while rounds < 2:
            key = self.keys[0]
            req = urllib.request.Request(
                f"{self.base}/embeddings",
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"})
            if key:
                req.add_header("Authorization", "Bearer " + key)
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    d = json.loads(r.read().decode())
                if len(self.keys) > 1:
                    self.keys.append(self.keys.pop(0))   # round-robin
                return self._parse_embeddings(d, texts)
            except urllib.error.HTTPError as ex:
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
                    throttled = set()
                    time.sleep(wait)
        raise RuntimeError("embeddings endpoint: all keys throttled for 2 rounds")

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
            for i in range(0, total, BATCH):
                chunk = todo[i:i + BATCH]
                try:
                    vecs = self._post(chunk)
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
        names = names[:limit]
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
    assert _map(0.50) == 0.0 and _map(0.72) == 1.0
    assert _map(0.55) < _map(0.65) < _map(0.70), "rescale must be monotonic"

    tmp = tempfile.mkdtemp(prefix="dsh_embed_")
    cfg = {"ai": {"enabled": True, "semantic_matching": True,
                  "embedding_model": "fake", "embedding_dims": 4}}

    # Fake vectors mirror LIVE gemini-embedding-001 cosines (09-05 probe):
    # same-product 0.672, category-adjacent 0.5745, unrelated 0.505 — so the
    # pass/fail assertions below sit on the real landscape, not invented
    # geometry. q = the query's vector ("diet coke").
    q = [1.0, 0.0, 0.0, 0.0]

    def _at(cos):
        return [cos, math.sqrt(1.0 - cos * cos), 0.0, 0.0]
    fake_vecs = {
        "Coca-Cola Zero Sugar 750ml": _at(0.672),
        "Kinley Soda Water Bottle":   _at(0.5745),
        "Dettol Original Soap":       _at(0.505),
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
    shutil.rmtree(tmp, ignore_errors=True)
    print("[embed] self-test OK: geometry, rescale, boost, disabled path, "
          "persistence (5 checks, 0 network calls after backfill)")
