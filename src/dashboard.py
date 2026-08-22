"""
dashboard.py — local web UI to watch the process in real time and stop it.

Stdlib only (http.server). Endpoints:
    GET  /          the single-page UI (tools/dashboard.html)
    GET  /status    JSON snapshot (counters + live event stream)
    POST /search    {"query": "..."} — run a cross-platform search now
    POST /shutdown  gracefully exit the whole process

Binds 127.0.0.1 only. Default port 8787 (config.yaml → ui.port).
"""
from __future__ import annotations

import json
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import events
from .search import SearchEngine, format_reply

_HTML_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "tools", "dashboard.html")


class Dashboard:
    def __init__(self, cfg):
        self.cfg = cfg
        self.engine = None
        self._engine_lock = threading.Lock()
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
                    self._json({"ok": True})
                    threading.Timer(0.4, lambda: os.kill(os.getpid(), signal.SIGINT)).start()

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
            self.httpd.server_close()
            print("[ui] shutdown complete")
