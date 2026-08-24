"""
dashboard.py — local web UI to watch the process in real time and stop it.

Stdlib only (http.server). Endpoints:
    GET  /          the single-page UI (tools/dashboard.html)
    GET  /status    JSON snapshot (counters + live event stream)
    POST /search    {"query": "..."} — run a cross-platform search now
    GET  /db        all sqlite databases (deals.db + inventory_*.db):
                    per-table row counts
    GET  /db/<db>/<table>?limit=N   recent rows of one table (read-only)
    GET  /features                  feature catalog + running state
    GET  /features/<id>/log         captured output of a managed feature
    POST /features/<id>/start       spawn a feature as a managed subprocess
    POST /features/<id>/stop        terminate it (SIGTERM, then SIGKILL)
    POST /shutdown  gracefully exit the whole process (+ stops all features)

Binds 127.0.0.1 only. Default port 8787 (config.yaml → ui.port).
"""
from __future__ import annotations

import collections
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import events
from .search import SearchEngine, format_reply

_HTML_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "tools", "dashboard.html")
_ROOT = os.path.dirname(os.path.dirname(_HTML_PATH))   # repo root: deals.db lives here

# Every capability of this repo, as startable dashboard actions. Services run
# until stopped; tasks are one-shot runs. Commands are plain `run.py` flag
# vectors so the panel always mirrors the real CLI.
FEATURE_CATALOG = [
    {"id": "monitor", "label": "Glitch monitor", "service": True,
     "desc": "Crawls Blinkit · Zepto · Instamart across the Mumbai corridor "
             "(Virar→Andheri), scores price glitches and pushes Telegram "
             "alerts for real mispricings.",
     "meta": "LIVE crawl of all 3 apps · writes deals.db + alerts · watch the Live feed below",
     "cmd": [sys.executable, "-u", "run.py"]},
    {"id": "bot", "label": "Telegram bot", "service": True,
     "desc": "Answers /search with the cheapest offer across apps, pushes "
             "/watch keyword alerts when crawls match, /digest for today's "
             "best finds per category.",
     "meta": "needs TG_BOT_TOKEN in .env · replies land in your Telegram chat",
     "cmd": [sys.executable, "-u", "run.py", "--bot", "--no-monitor"]},
    {"id": "demand", "label": "Demand prober", "service": True,
     "desc": "Continuously probes every watchlist SKU on its darkstore, "
             "records stock/price/ETA observations and opens debounced "
             "stock-out events. Feeds the Demand Radar panels below.",
     "meta": "LIVE crawl, runs until stopped · keep it the only crawler running",
     "cmd": [sys.executable, "-u", "run.py", "--demand"]},
    {"id": "demo", "label": "Demo pipeline", "service": False,
     "desc": "Offline end-to-end test: injects a fake glitch into a synthetic "
             "store and verifies crawl → detect → alert → store. Touches no "
             "live app.",
     "meta": "SAFE anytime · ~10 s · use as a health check",
     "cmd": [sys.executable, "-u", "run.py", "--demo"]},
    {"id": "qc_status", "label": "QC health probe", "service": False,
     "desc": "One live probe per quick-commerce app to verify extraction "
             "still works: products found, stock states, store id and ETA.",
     "meta": "light live check · ~45 s per app",
     "cmd": [sys.executable, "-u", "run.py", "--qc-status"]},
    {"id": "demand_once", "label": "Demand round", "service": False,
     "desc": "A single full demand-probe sweep across watchlist stores, then "
             "stops — collects the same data as the Demand prober without "
             "looping forever.",
     "meta": "live crawl · ~70 s · lighter alternative to the prober",
     "cmd": [sys.executable, "-u", "run.py", "--demand", "--once", "--max-terms", "5"]},
    {"id": "store_inventory", "label": "Store inventory", "service": False,
     "desc": "Maps darkstores near this machine's REAL location (public IP, "
             "no spoofing) into inventory_<app>.db per app AND captures every "
             "probe's products — browse them in SQL databases below.",
     "meta": "LIVE crawl · ~10–15 min for all 3 apps · run ALONE — concurrent "
             "crawls get rate-limited into empty results",
     "cmd": [sys.executable, "-u", "run.py", "--store-inventory"]},
    {"id": "map_locality", "label": "Map locality", "service": False,
     "desc": "Discovers darkstores for the configured locality (Andheri West) "
             "anchor-by-anchor into deals.db and exports rotation-pool JSONs.",
     "meta": "LIVE crawl · ~2 min+ per app",
     "cmd": [sys.executable, "-u", "run.py", "--map-locality"]},
    {"id": "build_watchlist", "label": "Build watchlist", "service": False,
     "desc": "Builds per-store SKU probe sets from live category/search "
             "sweeps — the list of items the Demand prober then tracks for "
             "stock-outs.",
     "meta": "LIVE crawl · writes the watchlist table in deals.db",
     "cmd": [sys.executable, "-u", "run.py", "--build-watchlist"]},
    {"id": "demand_report", "label": "Demand report", "service": False,
     "desc": "Prints the Demand Pressure Index ranking and hour×SKU onset "
             "heatmap summary computed from already-recorded data.",
     "meta": "NO crawling · safe anytime · --csv exports exports/dpi_*.csv",
     "cmd": [sys.executable, "-u", "run.py", "--demand-report"]},
]


class FeatureManager:
    """Start/stop repo features as child processes; capture their output.

    Each feature gets a ring buffer of stdout+stderr lines (children launch
    unbuffered via -u), a pid and exit code. Everything still running is
    terminated when the dashboard shuts down — never orphan crawlers.
    """

    LOG_LINES = 400

    def __init__(self, catalog=None, cwd=_ROOT):
        self.catalog = {f["id"]: dict(f) for f in (catalog or FEATURE_CATALOG)}
        self.cwd = cwd
        self._lock = threading.Lock()
        self.procs = {}   # fid -> {proc, log: deque, started_ts, returncode}

    # -- queries ----------------------------------------------------------
    def status(self):
        out = []
        with self._lock:
            for fid, f in self.catalog.items():
                st = self.procs.get(fid)
                running = bool(st and st["proc"].poll() is None)
                out.append({
                    "id": fid, "label": f["label"], "desc": f["desc"],
                    "meta": f.get("meta", ""),
                    "service": f["service"],
                    "running": running,
                    "pid": st["proc"].pid if running else None,
                    "started_ts": st["started_ts"] if st else None,
                    "uptime_sec": round(time.time() - st["started_ts"]) if running else None,
                    "returncode": (st["returncode"] if st and not running
                                   else None),
                    "finished_ok": (st["returncode"] == 0) if st and not running else None,
                })
        return {"features": out}

    def log_tail(self, fid, limit=120):
        with self._lock:
            st = self.procs.get(fid)
            lines = list(st["log"]) if st else []
        running = bool(st and st["proc"].poll() is None)
        return {"id": fid, "running": running,
                "lines": lines[-max(1, min(limit, self.LOG_LINES)):],
                "returncode": st["returncode"] if st and not running else None}

    # -- control ----------------------------------------------------------
    def start(self, fid):
        f = self.catalog.get(fid)
        if not f:
            return {"error": f"unknown feature '{fid}'"}, 404
        with self._lock:
            st = self.procs.get(fid)
            if st and st["proc"].poll() is None:
                return {"error": f"'{fid}' is already running (pid {st['proc'].pid})"}, 409
            try:
                proc = subprocess.Popen(
                    f["cmd"], cwd=self.cwd,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1,
                )
            except Exception as ex:
                return {"error": f"spawn failed: {str(ex)[:160]}"}, 500
            self.procs[fid] = {
                "proc": proc, "started_ts": time.time(), "returncode": None,
                "log": collections.deque(maxlen=self.LOG_LINES),
            }
            threading.Thread(target=self._pump, args=(fid, proc),
                             daemon=True).start()
        events.emit("system", f"▶️ started {f['label']} (pid {proc.pid})")
        return {"ok": True, "pid": proc.pid}, 200

    def stop(self, fid):
        with self._lock:
            st = self.procs.get(fid)
            if not st or st["proc"].poll() is not None:
                return {"error": f"'{fid}' is not running"}, 409
            proc = st["proc"]
        events.emit("system", f"⏹ stopping {self.catalog.get(fid, {}).get('label', fid)} "
                              f"(pid {proc.pid})")
        try:
            proc.terminate()                      # SIGTERM
            try:
                proc.wait(timeout=6)
            except subprocess.TimeoutExpired:
                proc.kill()                       # SIGKILL fallback
                proc.wait(timeout=5)
        except Exception:
            pass
        return {"ok": True}, 200

    def stop_all(self):
        for fid in list(self.procs):
            self.stop(fid)

    # -- internals --------------------------------------------------------
    def _pump(self, fid, proc):
        st = self.procs.get(fid)
        log = st["log"] if st else collections.deque(maxlen=self.LOG_LINES)
        try:
            for line in iter(proc.stdout.readline, ""):
                if line:
                    log.append(line.rstrip("\n")[:300])
        except Exception:
            pass
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass
            rc = proc.wait()
            with self._lock:
                if fid in self.procs:
                    self.procs[fid]["returncode"] = rc
            label = self.catalog.get(fid, {}).get("label", fid)
            events.emit("system", f"■ {label} exited (rc={rc})")


def _sqlite_files():
    """All repo-root sqlite databases worth browsing (sidecars excluded)."""
    out = []
    for name in sorted(os.listdir(_ROOT)):
        if not name.endswith(".db"):
            continue
        path = os.path.join(_ROOT, name)
        if not os.path.isfile(path):
            continue
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            counts = {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                      for t in tables}
            conn.close()
        except Exception as ex:
            tables, counts = [], {"_error": str(ex)[:80]}
        out.append({
            "name": name,
            "size_kb": round(os.path.getsize(path) / 1024, 1),
            "modified": time.strftime("%d %b %H:%M", time.localtime(os.path.getmtime(path))),
            "tables": [{"name": t, "rows": counts.get(t)} for t in tables]
            if not counts.get("_error") else [],
            "error": counts.get("_error"),
        })
    return {"databases": out}


def _table_preview(db_name, table, limit=30):
    """Recent rows of a table, read-only. Names validated against the DB's own
    schema before any interpolation."""
    if not db_name.endswith(".db") or "/" in db_name or "\\" in db_name or ".." in db_name:
        return {"error": "bad database name"}, 400
    path = os.path.join(_ROOT, db_name)
    if not os.path.isfile(path):
        return {"error": "no such database"}, 404
    limit = max(1, min(int(limit or 30), 200))
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        known = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        if table not in known:
            conn.close()
            return {"error": f"no such table '{table}'"}, 404
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
        rows = conn.execute(
            f'SELECT * FROM "{table}" ORDER BY rowid DESC LIMIT ?', (limit,)).fetchall()
        conn.close()
    except Exception as ex:
        return {"error": str(ex)[:200]}, 500
    return {"database": db_name, "table": table, "columns": cols,
            "rows": [[(c if c is not None else None) for c in r] for r in rows]}, 200


class Dashboard:
    def __init__(self, cfg):
        self.cfg = cfg
        self.engine = None
        self._engine_lock = threading.Lock()
        self.features = FeatureManager()
        port = int(cfg.get("ui", {}).get("port", 8787))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), self._make_handler())
        self.port = port

    def _engine(self):
        with self._engine_lock:
            if self.engine is None:
                self.engine = SearchEngine(self.cfg)
            return self.engine

    def _make_handler(self):
        dash = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet default access log
                pass

            def _json(self, obj, code=200):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path in ("/", "/index.html"):
                    try:
                        html = open(_HTML_PATH, "rb").read()
                    except OSError:
                        self._json({"error": "dashboard.html missing"}, 500)
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(html)))
                    self.end_headers()
                    self.wfile.write(html)
                elif self.path == "/status":
                    snap = events.snapshot()
                    snap["bot"] = {"username": dash.cfg.get("_bot_username", ""),
                                   "token_present": bool(
                                       os.environ.get("TG_BOT_TOKEN") or
                                       __import__("src.alert", fromlist=["_load_env"])._load_env().get("TG_BOT_TOKEN"))}
                    self._json(snap)
                elif self.path == "/categories":
                    try:
                        from .store import Store
                        st = Store(dash.cfg.get("db", "deals.db"))
                        self._json({"categories": st.category_stats()})
                        st.close()
                    except Exception as ex:
                        self._json({"error": str(ex)[:200], "categories": []})
                elif self.path == "/searches":
                    try:
                        from .store import Store
                        st = Store(dash.cfg.get("db", "deals.db"))
                        self._json({"searches": st.recent_searches(25)})
                        st.close()
                    except Exception as ex:
                        self._json({"error": str(ex)[:200], "searches": []})
                elif self.path == "/features":
                    self._json(dash.features.status())
                elif self.path.startswith("/features/") and self.path.endswith("/log"):
                    fid = self.path[len("/features/"):-len("/log")]
                    self._json(dash.features.log_tail(fid))
                elif self.path == "/db":
                    try:
                        self._json(_sqlite_files())
                    except Exception as ex:
                        self._json({"error": str(ex)[:200], "databases": []})
                elif self.path.startswith("/db/"):
                    # /db/<name>/<table>?limit=N
                    from urllib.parse import urlparse, parse_qs
                    parts = urlparse(self.path)
                    seg = [s for s in parts.path.split("/") if s]  # ['db', name, table]
                    if len(seg) != 3:
                        self._json({"error": "use /db/<name>/<table>"}, 400)
                        return
                    limit = (parse_qs(parts.query).get("limit") or [30])[0]
                    try:
                        limit = int(limit)
                    except ValueError:
                        limit = 30
                    data, code = _table_preview(seg[1], seg[2], limit)
                    self._json(data, code)
                # ---- Demand Radar (phase 4) ----
                elif self.path == "/demand":
                    try:
                        from .store import Store as S
                        from . import demand as D
                        st = S(dash.cfg.get("db", "deals.db"))
                        self._json({"summary": D.demand_summary(st),
                                    "top": D.dpi_table(st, limit=15)})
                        st.close()
                    except Exception as ex:
                        self._json({"error": str(ex)[:200]})
                elif self.path.startswith("/heatmap"):
                    try:
                        from urllib.parse import urlparse, parse_qs
                        qs = parse_qs(urlparse(self.path).query)
                        sid = (qs.get("store") or [None])[0]
                        from .store import Store as S
                        from . import demand as D
                        st = S(dash.cfg.get("db", "deals.db"))
                        self._json(D.heatmap(st, store_id=sid))
                        st.close()
                    except Exception as ex:
                        self._json({"error": str(ex)[:200], "skus": []})
                elif self.path == "/eta":
                    try:
                        from .store import Store as S
                        from . import demand as D
                        st = S(dash.cfg.get("db", "deals.db"))
                        self._json({"curve": D.eta_curve(st)})
                        st.close()
                    except Exception as ex:
                        self._json({"error": str(ex)[:200], "curve": []})
                elif self.path == "/qc":
                    # DB-backed platform health (no live probing here — use
                    # `run.py --qc-status` for that; it takes ~45 s/app).
                    try:
                        from .store import Store as S
                        st = S(dash.cfg.get("db", "deals.db"))
                        day_ago = time.time() - 86400
                        apps = []
                        for a in ("blinkit", "zepto", "instamart"):
                            en = bool(dash.cfg.get("adapters", {}).get(a, {}).get("enabled", True))
                            q = lambda sql, *p: st.conn.execute(sql, p).fetchone()[0]
                            apps.append({
                                "app": a, "enabled": en,
                                "stores": q("SELECT COUNT(*) FROM darkstores WHERE app=?", a),
                                "obs_24h": q("SELECT COUNT(*) FROM stock_obs WHERE ts>? AND app=?", day_ago, a),
                                "open_oos": q("SELECT COUNT(*) FROM oos_events WHERE ended_at IS NULL "
                                              "AND kind='oos' AND app=?", a),
                                "watch_active": q("SELECT COUNT(*) FROM watchlist WHERE active=1 AND app=?", a),
                            })
                        self._json({"apps": apps})
                        st.close()
                    except Exception as ex:
                        self._json({"error": str(ex)[:200], "apps": []})
                elif self.path.startswith("/search/") and self.path.count("/") == 2:
                    try:
                        sid = int(self.path.rsplit("/", 1)[1])
                    except ValueError:
                        self._json({"error": "bad id"}, 400)
                        return
                    try:
                        from .store import Store
                        st = Store(dash.cfg.get("db", "deals.db"))
                        data = st.get_search(sid)
                        st.close()
                    except Exception as ex:
                        self._json({"error": str(ex)[:200]}, 500)
                        return
                    if not data:
                        self._json({"error": "not found"}, 404)
                    else:
                        self._json(data)
                else:
                    self._json({"error": "not found"}, 404)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b"{}"
                try:
                    payload = json.loads(raw.decode() or "{}")
                except Exception:
                    payload = {}

                if self.path == "/shutdown":
                    events.emit("system", "🛑 shutdown requested from dashboard")
                    dash.features.stop_all()
                    self._json({"ok": True})
                    # Deterministic exit: stop_all() reaped the children above,
                    # the response is flushed, remaining threads are daemons.
                    threading.Timer(0.4, lambda: os._exit(0)).start()

                elif self.path.startswith("/features/") and self.path.endswith("/start"):
                    fid = self.path[len("/features/"):-len("/start")]
                    data, code = dash.features.start(fid)
                    self._json(data, code)

                elif self.path.startswith("/features/") and self.path.endswith("/stop"):
                    fid = self.path[len("/features/"):-len("/stop")]
                    data, code = dash.features.stop(fid)
                    self._json(data, code)

                elif self.path == "/search":
                    query = (payload.get("query") or "").strip()
                    if not query:
                        self._json({"error": "query required"}, 400)
                        return
                    t0 = time.time()
                    res = dash._engine().search(query, source="dashboard")
                    top = res["results"][0] if res["results"] else None
                    events.bump("queries")
                    events.emit("reply",
                                (f"{top['platform']} ₹{top['effective']:.0f} — {top['name'][:40]}"
                                 if top else f"no matches for “{query}”"),
                                query=query, top=top,
                                results=res.get("results", [])[:10],
                                search_id=res.get("search_id"),
                                ui=True, elapsed=res.get("elapsed"))
                    self._json({"formatted": format_reply(res),
                                "results": res["results"][:10],
                                "search_id": res.get("search_id"),
                                "elapsed": round(time.time() - t0, 1)})
                else:
                    self._json({"error": "not found"}, 404)

        return Handler

    def serve_forever(self):
        events.emit("system", f"🖥️ dashboard on http://127.0.0.1:{self.port}")
        print(f"[ui] dashboard → http://127.0.0.1:{self.port}  (Ctrl-C or STOP button quits)")
        try:
            self.httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self.features.stop_all()   # never orphan managed crawlers/bots
            self.httpd.server_close()
            print("[ui] shutdown complete")
