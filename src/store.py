"""
store.py — persistence: price history + per-(store,sku) baseline.

We keep a rolling window of observed prices per (app, store_id, sku_key) so the
detector can compute a z-score against the *local* norm. Quick-commerce promos
are local, so a global baseline would cry wolf constantly.
"""
from __future__ import annotations

import sqlite3
import statistics
import threading
import time
import os
from collections import defaultdict

from .categories import categorize

# Digital vouchers / gift cards are NOT commodities: their "stock-outs" are
# code-pool replenishment cycles, not shelf demand, so they poison the Demand
# Radar (they once held 15 of the top-20 DPI slots). One shared matcher so the
# watchlist builder, prober and DPI rollups all draw the same line. Kept to
# name tokens only — verified 09-02: every voucher SKU name on Blinkit carries
# "voucher" or "gift card"; NO real grocery/retail SKU does (0 false
# positives over the whole price_obs corpus), so no brand keywords needed.
VOUCHER_TOKENS = ("voucher", "gift card")


def is_voucher_name(name):
    """True when a product name is a digital voucher / gift-card SKU."""
    n = (name or "").lower()
    return any(t in n for t in VOUCHER_TOKENS)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS price_obs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,
    app TEXT,
    store_id TEXT,
    sku_key TEXT,
    name TEXT,
    price REAL,
    mrp REAL,
    url TEXT,
    category TEXT,
    catalog_version TEXT DEFAULT NULL
);
CREATE INDEX IF NOT EXISTS idx_obs ON price_obs(app, store_id, sku_key, ts);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,
    app TEXT,
    store_id TEXT,
    sku_key TEXT,
    reason TEXT,
    price REAL,
    score REAL,
    mrp REAL                  -- 09-02: reference price shown on alerts (NULL = unknown)
);
CREATE TABLE IF NOT EXISTS darkstores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,
    app TEXT,
    store_id TEXT,
    label TEXT,
    lat REAL,
    lon REAL,
    eta_min REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_darkstore ON darkstores(app, store_id);
CREATE TABLE IF NOT EXISTS watchlist (
    app TEXT,
    store_id TEXT,
    sku_key TEXT,
    name TEXT,
    collections TEXT,          -- 'home', category names, 'q:<term>' (csv)
    last_price REAL,
    last_in_stock INTEGER,     -- 1/0/null at build time
    last_seen_ts REAL,
    score REAL DEFAULT 0,      -- curation rank (higher = keep)
    active INTEGER DEFAULT 1,  -- probe-set membership (phase-3 prober reads this)
    is_digital_voucher INTEGER DEFAULT 0,
    PRIMARY KEY (app, store_id, sku_key)
);
CREATE INDEX IF NOT EXISTS idx_wl_active ON watchlist(app, store_id, active);
CREATE TABLE IF NOT EXISTS stock_obs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,
    app TEXT,
    store_id TEXT,
    sku_key TEXT,
    in_stock INTEGER,          -- 1/0/null (null = unknown: failed parse etc.)
    price REAL,
    mrp REAL,
    eta_min REAL,
    source TEXT,               -- 'home' | 'collection' | 'search'
    restock_trigger TEXT DEFAULT NULL,
    voucher_type TEXT DEFAULT NULL,
    promotional_context TEXT DEFAULT NULL,
    catalog_version TEXT DEFAULT NULL
);
CREATE INDEX IF NOT EXISTS idx_so ON stock_obs(store_id, sku_key, ts);
CREATE TABLE IF NOT EXISTS oos_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app TEXT,
    store_id TEXT,
    sku_key TEXT,
    kind TEXT DEFAULT 'oos',   -- 'oos' | 'vanished'
    started_at REAL,
    ended_at REAL,             -- NULL = ongoing
    snapshots INTEGER,          -- consecutive OOS obs when open/closed
    restock_trigger TEXT DEFAULT NULL
);
CREATE INDEX IF NOT EXISTS idx_oos ON oos_events(store_id, sku_key, started_at);
CREATE TABLE IF NOT EXISTS searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,
    query TEXT,
    source TEXT,               -- 'telegram' | 'dashboard' | 'cli'
    elapsed REAL,
    n_results INTEGER,
    top_platform TEXT,
    top_name TEXT,
    top_effective REAL
);
CREATE TABLE IF NOT EXISTS search_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    search_id INTEGER REFERENCES searches(id),
    rank INTEGER,
    platform TEXT,
    name TEXT,
    category TEXT,
    match REAL,
    mrp REAL,
    listed REAL,
    discount REAL,
    code TEXT,
    net REAL,
    delivery REAL,
    effective REAL,
    url TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_sr ON search_results(search_id);
CREATE INDEX IF NOT EXISTS idx_sr_cat ON search_results(category);
CREATE TABLE IF NOT EXISTS keyword_watches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT,              -- telegram chat to push matches to
    keyword TEXT,              -- lowercase watch phrase (token-overlap match)
    created_ts REAL,
    active INTEGER DEFAULT 1,  -- 0 after /unwatch; rows are never deleted
    last_alerted_ts REAL,      -- last successful push (per-watch cooldown basis)
    UNIQUE(chat_id, keyword)
);
CREATE INDEX IF NOT EXISTS idx_kww_active ON keyword_watches(active);
"""


class Store:
    def __init__(self, path="deals.db"):
        self.path = path
        self.lock = threading.Lock()   # search persistence can come from any thread
        self.conn = sqlite3.connect(path, check_same_thread=False)
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        self.conn.executescript(_SCHEMA)
        # migration: category column on price_obs
        try:
            self.conn.execute("ALTER TABLE price_obs ADD COLUMN category TEXT")
        except Exception:
            pass  # already exists
        # migration: expanded tracking parameters for digital vouchers / restock triggers
        migrations = [
            ("stock_obs", "restock_trigger", "TEXT DEFAULT NULL"),
            ("stock_obs", "voucher_type", "TEXT DEFAULT NULL"),
            ("stock_obs", "promotional_context", "TEXT DEFAULT NULL"),
            ("stock_obs", "catalog_version", "TEXT DEFAULT NULL"),
            ("watchlist", "is_digital_voucher", "INTEGER DEFAULT 0"),
            ("oos_events", "restock_trigger", "TEXT DEFAULT NULL"),
            ("price_obs", "catalog_version", "TEXT DEFAULT NULL"),
            ("alerts", "mrp", "REAL"),          # 09-02: MRP/usual shown on alerts
        ]
        for table, col, col_type in migrations:
            try:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
            except Exception:
                pass  # already exists
        self.conn.commit()
        self._backfill_categories()

    def _backfill_categories(self):
        """Classify any price_obs rows that predate categorization."""
        try:
            nul = self.conn.execute(
                "SELECT COUNT(*) FROM price_obs WHERE category IS NULL").fetchone()[0]
            if not nul:
                return
            rows = self.conn.execute(
                "SELECT id, name FROM price_obs WHERE category IS NULL").fetchall()
            self.conn.executemany(
                "UPDATE price_obs SET category=? WHERE id=?",
                [(categorize(name), i) for i, name in rows],
            )
            self.conn.commit()
        except Exception:
            pass

    def record(self, app, store_id, sku_key, name, price, mrp=None, url="", category=None):
        if not category:
            category = categorize(name)
        self.conn.execute(
            "INSERT INTO price_obs(ts,app,store_id,sku_key,name,price,mrp,url,category) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (time.time(), app, store_id, sku_key, name, price, mrp, url, category),
        )
        self.conn.commit()

    def record_alert(self, app, store_id, sku_key, reason, price, score, mrp=None):
        self.conn.execute(
            "INSERT INTO alerts(ts,app,store_id,sku_key,reason,price,score,mrp) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (time.time(), app, store_id, sku_key, reason, price, score, mrp),
        )
        self.conn.commit()

    def usual_price(self, app, store_id, sku_key,
                    span_seconds=7 * 24 * 3600, limit=200):
        """Median of this (store,sku)'s recent price_obs EXCLUDING the newest row.

        The newest observation is the one that just triggered the alert, so it
        must not define its own 'usual'. None when there is no prior history
        (then alerts fall back to MRP only)."""
        rows = self.conn.execute(
            "SELECT price FROM price_obs WHERE app=? AND store_id=? AND sku_key=? "
            "AND ts > ? ORDER BY ts DESC LIMIT ?",
            (app, store_id, sku_key, time.time() - span_seconds, limit + 1),
        ).fetchall()
        hist = [r[0] for r in rows[1:] if r[0] and r[0] > 0]
        if not hist:
            return None
        return statistics.median(hist)

    def upsert_darkstore(self, app, store_id, label, lat, lon, eta_min=None):
        """Demand Radar: register/refresh a resolved darkstore for an app."""
        self.conn.execute(
            "INSERT INTO darkstores(ts,app,store_id,label,lat,lon,eta_min) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(app, store_id) DO UPDATE SET ts=excluded.ts, label=excluded.label, "
            "lat=excluded.lat, lon=excluded.lon, eta_min=COALESCE(excluded.eta_min, darkstores.eta_min)",
            (time.time(), app, store_id, label, lat, lon, eta_min),
        )
        self.conn.commit()

    def darkstores(self, app=None):
        if app:
            return self.conn.execute(
                "SELECT app,store_id,label,lat,lon,eta_min FROM darkstores WHERE app=? ORDER BY app,label",
                (app,),
            ).fetchall()
        return self.conn.execute(
            "SELECT app,store_id,label,lat,lon,eta_min FROM darkstores ORDER BY app,label"
        ).fetchall()

    # ---- Demand Radar: watchlist (phase 2) ----
    def upsert_watchlist(self, app, store_id, rows):
        """
        rows: [{sku_key, name, collections:[str], price, in_stock, score}].
        Upserts everything seen; `active` is set by the caller's curation
        decision (top-N = 1, overflow = 0). Never deletes history.
        """
        now = time.time()
        for r in rows:
            is_voucher = 1 if is_voucher_name(r.get("name")) else 0
            self.conn.execute(
                "INSERT INTO watchlist(app,store_id,sku_key,name,collections,last_price,"
                "last_in_stock,last_seen_ts,score,active,is_digital_voucher) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(app, store_id, sku_key) DO UPDATE SET name=excluded.name, "
                "collections=excluded.collections, last_price=COALESCE(excluded.last_price, watchlist.last_price), "
                "last_in_stock=COALESCE(excluded.last_in_stock, watchlist.last_in_stock), "
                "last_seen_ts=excluded.last_seen_ts, score=excluded.score, active=excluded.active, "
                "is_digital_voucher=excluded.is_digital_voucher",
                (app, store_id, r["sku_key"], r.get("name"),
                 ",".join(r.get("collections") or []),
                 r.get("price"),
                 None if r.get("in_stock") is None else int(r["in_stock"]),
                 now, r.get("score", 0), 1 if r.get("active", True) else 0,
                 is_voucher),
            )
        self.conn.commit()

    def deactivate_watchlist_except(self, app, store_id, sku_keys):
        """Mark SKUs not in the current sweep as inactive (stale probe set)."""
        keys = list(sku_keys)
        if not keys:
            return
        qmarks = ",".join("?" * len(keys))
        self.conn.execute(
            f"UPDATE watchlist SET active=0 WHERE app=? AND store_id=? AND sku_key NOT IN ({qmarks})",
            [app, store_id] + keys,
        )
        self.conn.commit()

    # ---- Demand Radar: voucher purge (09-02) -------------------------------
    def purge_vouchers(self, dry_run=False):
        """
        Remove voucher / gift-card SKUs from every Demand Radar table
        (watchlist, stock_obs, oos_events). Idempotent; additive-schema safe
        (columns stay, only ROWS go). The voucher id set is derived from the
        watchlist's own classification (is_digital_voucher flag OR name
        match) and applied app+sku scoped, so sibling-store observations of
        the same product id are caught too. Returns {table: rows_affected}.
        """
        ids = self.conn.execute(
            "SELECT DISTINCT app, sku_key FROM watchlist "
            "WHERE is_digital_voucher=1 OR name LIKE '%voucher%' "
            "OR name LIKE '%gift card%'"
        ).fetchall()
        if not ids:
            return {}
        self.conn.execute(
            "CREATE TEMP TABLE IF NOT EXISTS _voucher_ids(app TEXT, sku_key TEXT)")
        self.conn.execute("DELETE FROM _voucher_ids")
        self.conn.executemany("INSERT INTO _voucher_ids VALUES(?,?)", ids)
        out = {}
        for table in ("watchlist", "stock_obs", "oos_events"):
            scope = f"WHERE (app, sku_key) IN (SELECT app, sku_key FROM _voucher_ids)"
            if dry_run:
                out[table] = self.conn.execute(
                    f"SELECT COUNT(*) FROM {table} {scope}").fetchone()[0]
            else:
                out[table] = self.conn.execute(f"DELETE FROM {table} {scope}").rowcount
        if not dry_run:
            self.conn.commit()
        self.conn.execute("DROP TABLE IF EXISTS _voucher_ids")
        return out

    def watchlist_for_store(self, app, store_id, active_only=True):
        q = ("SELECT sku_key,name,collections,last_price,last_in_stock,last_seen_ts,score "
             "FROM watchlist WHERE app=? AND store_id=?")
        if active_only:
            q += " AND active=1"
        q += " ORDER BY score DESC, name"
        return self.conn.execute(q, (app, store_id)).fetchall()

    def watchlist_stats(self):
        return self.conn.execute(
            "SELECT app,store_id,COUNT(*),SUM(active) FROM watchlist "
            "GROUP BY app,store_id ORDER BY app,store_id"
        ).fetchall()

    def watchlist_terms(self, app, store_id):
        """Distinct 'q:' search labels among ACTIVE rows, most-covering first."""
        rows = self.conn.execute(
            "SELECT collections FROM watchlist WHERE app=? AND store_id=? AND active=1",
            (app, store_id),
        ).fetchall()
        freq = {}
        for (csv,) in rows:
            for c in (csv or "").split(","):
                c = c.strip()
                if c.startswith("q:"):
                    freq[c[2:]] = freq.get(c[2:], 0) + 1
        return [t for t, _ in sorted(freq.items(), key=lambda kv: -kv[1])]

    # ---- Demand Radar: stock observations + OOS events (phase 3) ----
    def record_stock_obs(self, app, store_id, rows, eta_min=None, ts=None):
        """rows: [{sku_key, in_stock(bool|None), price, mrp, source,
                   restock_trigger, voucher_type, promotional_context, catalog_version}]"""
        now = ts if ts is not None else time.time()
        payload = []
        for r in rows:
            payload.append((
                now, app, store_id, r["sku_key"],
                None if r.get("in_stock") is None else int(r["in_stock"]),
                r.get("price"), r.get("mrp"), eta_min, r.get("source", "sweep"),
                r.get("restock_trigger"), r.get("voucher_type"),
                r.get("promotional_context"), r.get("catalog_version"),
            ))
        self.conn.executemany(
            "INSERT INTO stock_obs(ts,app,store_id,sku_key,in_stock,price,mrp,eta_min,source,"
            "restock_trigger,voucher_type,promotional_context,catalog_version) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", payload,
        )
        self.conn.commit()
        return len(payload)

    def open_oos_event(self, app, store_id, sku_key, started_at=None,
                       kind="oos"):
        """Open an event unless one of the same kind is already open."""
        row = self.conn.execute(
            "SELECT id FROM oos_events WHERE app=? AND store_id=? AND sku_key=? "
            "AND kind=? AND ended_at IS NULL",
            (app, store_id, sku_key, kind),
        ).fetchone()
        if row:
            return row[0]
        cur = self.conn.execute(
            "INSERT INTO oos_events(app,store_id,sku_key,kind,started_at,ended_at,snapshots) "
            "VALUES(?,?,?,?,?,NULL,NULL)",
            (app, store_id, sku_key, kind, started_at or time.time()),
        )
        self.conn.commit()
        return cur.lastrowid

    def close_oos_event(self, app, store_id, sku_key, snapshots=1,
                        kinds=("oos", "vanished"), ended_at=None,
                         restock_trigger=None):
        t = ended_at if ended_at is not None else time.time()
        qmarks = ",".join("?" * len(kinds))
        cur = self.conn.execute(
            f"UPDATE oos_events SET ended_at=?, snapshots=? WHERE app=? AND store_id=? "
            f"AND sku_key=? AND ended_at IS NULL AND kind IN ({qmarks})",
            [t, snapshots, app, store_id, sku_key] + list(kinds),
        )
        if restock_trigger and cur.rowcount > 0:
            cur2 = self.conn.execute(
                "UPDATE oos_events SET restock_trigger=? WHERE app=? AND store_id=? "
                "AND sku_key=? AND ended_at=?",
                (restock_trigger, app, store_id, sku_key, t),
            )
        self.conn.commit()
        return cur.rowcount

    def open_event_state(self, app, store_id, sku_key):
        """(kind, started_at) if an event is open, else None."""
        row = self.conn.execute(
            "SELECT kind,started_at FROM oos_events WHERE app=? AND store_id=? "
            "AND sku_key=? AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
            (app, store_id, sku_key),
        ).fetchone()
        return row if row else None

    def open_events_for_store(self, app, store_id, kind="oos"):
        """Open events of one kind for a single store: [(sku_key, started_at)].
        Used by the prober's stale-event reconciliation."""
        return self.conn.execute(
            "SELECT sku_key, started_at FROM oos_events WHERE app=? AND store_id=? "
            "AND kind=? AND ended_at IS NULL",
            (app, store_id, kind),
        ).fetchall()

    def last_obs_ts(self, app, store_id, sku_key):
        """Timestamp of the last recorded observation of a SKU at a store,
        or None if never seen. The honest horizon for event durations."""
        row = self.conn.execute(
            "SELECT MAX(ts) FROM stock_obs WHERE app=? AND store_id=? AND sku_key=?",
            (app, store_id, sku_key),
        ).fetchone()
        return row[0] if row and row[0] is not None else None

    def trailing_oos_streak(self, app, store_id, sku_key, lookback=12):
        """
        Reconstruct an OOS streak from recorded observations (restart-proof
        debounce): count of most-recent non-null readings that are 0, stopping
        at the first 1. Returns (count, first_ts_of_streak_or_None).
        """
        rows = self.conn.execute(
            "SELECT in_stock, ts FROM stock_obs WHERE app=? AND store_id=? AND sku_key=? "
            "AND in_stock IS NOT NULL ORDER BY ts DESC, id DESC LIMIT ?",
            (app, store_id, sku_key, lookback),
        ).fetchall()
        count, first_ts = 0, None
        for val, ts in rows:          # newest -> oldest
            if val == 0:
                count += 1
                first_ts = ts
            else:
                break
        return count, first_ts

    def oos_stats(self, since_seconds=24 * 3600):
        return self.conn.execute(
            "SELECT kind,COUNT(*),SUM(ended_at IS NULL) FROM oos_events "
            "WHERE started_at>? GROUP BY kind",
            (time.time() - since_seconds,),
        ).fetchall()

    def window(self, app, store_id, sku_key, span_seconds=7 * 24 * 3600, limit=200):
        """Recent prices for a (store,sku) baseline."""
        rows = self.conn.execute(
            "SELECT price FROM price_obs WHERE app=? AND store_id=? AND sku_key=? "
            "AND ts > ? ORDER BY ts DESC LIMIT ?",
            (app, store_id, sku_key, time.time() - span_seconds, limit),
        ).fetchall()
        return [r[0] for r in rows]

    def last_alerted(self, app, store_id, sku_key, since_seconds=3600):
        row = self.conn.execute(
            "SELECT ts FROM alerts WHERE app=? AND store_id=? AND sku_key=? AND ts>? "
            "ORDER BY ts DESC LIMIT 1",
            (app, store_id, sku_key, time.time() - since_seconds),
        ).fetchone()
        return row[0] if row else None

    def recent_alerts(self, limit=20):
        return self.conn.execute(
            "SELECT ts,app,store_id,reason,price,score,mrp FROM alerts ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()

    # ---- search-results archive (for future analysis) ----
    def save_search(self, query, source, elapsed, results):
        """Persist one search + every result row. Returns the search_id."""
        now = time.time()
        top = results[0] if results else None
        with self.lock:
            try:
                cur = self.conn.execute(
                    "INSERT INTO searches(ts,query,source,elapsed,n_results,"
                    "top_platform,top_name,top_effective) VALUES(?,?,?,?,?,?,?,?)",
                    (now, query, source, elapsed, len(results),
                     top and top.get("platform"), top and top.get("name"),
                     top and top.get("effective")),
                )
                sid = cur.lastrowid
                self.conn.executemany(
                    "INSERT INTO search_results(search_id,rank,platform,name,category,"
                    "match,mrp,listed,discount,code,net,delivery,effective,url,note) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(sid, i + 1, r.get("platform"), r.get("name"), r.get("category"),
                      r.get("match"), r.get("mrp"), r.get("listed"), r.get("discount"),
                      r.get("code"), r.get("net"), r.get("delivery"),
                      r.get("effective"), r.get("url"), r.get("note"))
                     for i, r in enumerate(results)],
                )
                self.conn.commit()
                return sid
            except Exception:
                return None

    def recent_searches(self, limit=25):
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, ts, query, source, elapsed, n_results, "
                "top_platform, top_name, top_effective "
                "FROM searches ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            {"id": r[0], "ts": r[1],
             "time": time.strftime("%H:%M:%S", time.localtime(r[1])),
             "query": r[2], "source": r[3], "elapsed": r[4], "n_results": r[5],
             "top_platform": r[6], "top_name": r[7], "top_effective": r[8]}
            for r in rows
        ]

    def get_search(self, search_id):
        with self.lock:
            srow = self.conn.execute(
                "SELECT id, ts, query, source, elapsed, n_results, top_platform, "
                "top_name, top_effective FROM searches WHERE id=?", (search_id,)
            ).fetchone()
            if not srow:
                return None
            rrows = self.conn.execute(
                "SELECT rank,platform,name,category,match,mrp,listed,discount,code,"
                "net,delivery,effective,url,note FROM search_results "
                "WHERE search_id=? ORDER BY effective", (search_id,)
            ).fetchall()
        keys = ["rank", "platform", "name", "category", "match", "mrp", "listed",
                "discount", "code", "net", "delivery", "effective", "url", "note"]
        return {
            "search": {"id": srow[0], "ts": srow[1],
                       "time": time.strftime("%H:%M:%S", time.localtime(srow[1])),
                       "query": srow[2], "source": srow[3], "elapsed": srow[4],
                       "n_results": srow[5], "top_platform": srow[6],
                       "top_name": srow[7], "top_effective": srow[8]},
            "results": [dict(zip(keys, r)) for r in rrows],
        }

    def category_stats(self):
        """Products covered per category across everything ever crawled."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT COALESCE(category,'Other') AS cat, "
                "COUNT(DISTINCT sku_key) AS products, COUNT(*) AS obs, "
                "MIN(price), MAX(price) "
                "FROM price_obs GROUP BY cat ORDER BY products DESC"
            ).fetchall()
        return [
            {"category": r[0], "products": r[1], "observations": r[2],
             "min_price": r[3], "max_price": r[4]}
            for r in rows
        ]

    # ---- Telegram keyword watches (proactive alerts) ----
    def add_watch(self, chat_id, keyword):
        """Register a keyword watch for a chat. Returns True if newly added."""
        kw = (keyword or "").strip().lower()
        if not kw:
            return False
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO keyword_watches(chat_id,keyword,created_ts,active,"
            "last_alerted_ts) VALUES(?,?,?,1,NULL)",
            (str(chat_id), kw, time.time()),
        )
        # reactivate a previously /unwatched keyword
        if cur.rowcount == 0:
            cur = self.conn.execute(
                "UPDATE keyword_watches SET active=1, last_alerted_ts=NULL "
                "WHERE chat_id=? AND keyword=? AND active=0",
                (str(chat_id), kw),
            )
        self.conn.commit()
        return cur.rowcount > 0

    def remove_watch(self, chat_id, keyword):
        """Deactivate a watch (history preserved). Returns True if it was active."""
        kw = (keyword or "").strip().lower()
        cur = self.conn.execute(
            "UPDATE keyword_watches SET active=0 WHERE chat_id=? AND keyword=? AND active=1",
            (str(chat_id), kw),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def watches_for_chat(self, chat_id):
        """Active [(keyword, created_ts, last_alerted_ts)] for one chat, oldest first."""
        return self.conn.execute(
            "SELECT keyword, created_ts, last_alerted_ts FROM keyword_watches "
            "WHERE chat_id=? AND active=1 ORDER BY created_ts, id",
            (str(chat_id),),
        ).fetchall()

    def active_watches(self):
        """All active watches: [(id, chat_id, keyword, last_alerted_ts)]."""
        return self.conn.execute(
            "SELECT id, chat_id, keyword, last_alerted_ts FROM keyword_watches "
            "WHERE active=1 ORDER BY created_ts, id"
        ).fetchall()

    def mark_watch_alerted(self, watch_id, ts=None):
        self.conn.execute(
            "UPDATE keyword_watches SET last_alerted_ts=? WHERE id=?",
            (ts if ts is not None else time.time(), watch_id),
        )
        self.conn.commit()

    def today_cheapest_by_category(self, day_start, limit=8):
        """
        Cheapest effective find per category among searches archived since
        `day_start` (epoch). Pure read over searches/search_results — no
        crawling. Returns [{category, count, name, platform, effective}] with
        the category that had the most finds first.
        """
        with self.lock:
            rows = self.conn.execute(
                "SELECT COALESCE(r.category,'Other') AS cat, r.name, r.platform, r.effective "
                "FROM search_results r JOIN searches s ON s.id=r.search_id "
                "WHERE s.ts>=? AND r.effective IS NOT NULL",
                (day_start,),
            ).fetchall()
        best, counts = {}, {}
        for cat, name, platform, eff in rows:
            counts[cat] = counts.get(cat, 0) + 1
            if cat not in best or eff < best[cat][2]:
                best[cat] = (name, platform, eff)
        out = [{"category": c, "count": counts[c],
                "name": n, "platform": p, "effective": e}
               for c, (n, p, e) in best.items()]
        out.sort(key=lambda d: (-d["count"], d["category"]))
        return out[:limit]

    def close(self):
        self.conn.close()
