"""
dashboard.py — local web UI to watch the process in real time and stop it.

Stdlib only (http.server). Endpoints:
    GET  /          the single-page UI (tools/dashboard.html)
    GET  /status    JSON snapshot (counters + live event stream)
    POST /search    {"query": "..."} — run a cross-platform search now
    GET  /db        all sqlite databases (deals.db + inventory/*.db):
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
import math
import os
import re
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
#
# Per-feature argument editing (09-02): "args" declares the flags the
# Features panel may override for that feature. Each arg is
#   {"flag": "--store", "kind": "text"|"int"|"float"|"select"|"bool",
#    "label": ..., "ph": placeholder, "opts": ["a","b", …] (select only)}
# The POST /features/<id>/start body may carry {"args": {"--store": "34292"}};
# FeatureManager validates EVERY flag against this spec (unknown flags are
# rejected, never forwarded) and appends `flag value` to the command vector —
# base cmds keep any built-in flags (e.g. --demand --once) as the last word.
FEATURE_CATALOG = [
    {"id": "monitor", "label": "Glitch monitor", "service": True, "group": "monitor",
     "desc": "Crawls Blinkit · Zepto · Instamart across the Mumbai corridor "
             "(Virar→Andheri), scores price glitches and pushes Telegram "
             "alerts for real mispricings.",
     "meta": "LIVE crawl of all 3 apps · writes deals.db + alerts · watch the Live feed below",
     "cmd": [sys.executable, "-u", "run.py"]},
    {"id": "bot", "label": "Telegram bot", "service": True, "group": "monitor",
     "desc": "Answers /search with the cheapest offer across apps, pushes "
             "/watch keyword alerts when crawls match, /digest for today's "
             "best finds per category.",
     "meta": "needs TG_BOT_TOKEN in .env · replies land in your Telegram chat",
     "cmd": [sys.executable, "-u", "run.py", "--bot", "--no-monitor"]},
    {"id": "demand", "label": "Demand prober", "service": True, "group": "demand",
     "desc": "Continuously probes every watchlist SKU on its darkstore, "
             "records stock/price/ETA observations and opens debounced "
             "stock-out events. Feeds the Demand Radar panels below.",
     "meta": "LIVE crawl, runs until stopped · keep it the only crawler running",
     "cmd": [sys.executable, "-u", "run.py", "--demand"],
     "args": [
         {"flag": "--apps", "kind": "text", "label": "apps",
          "ph": "blinkit,zepto,instamart"},
         {"flag": "--store", "kind": "text", "label": "store id",
          "ph": "e.g. 34292"},
         {"flag": "--max-terms", "kind": "int", "label": "max terms/store"},
     ]},
    {"id": "demo", "label": "Demo pipeline", "service": False, "group": "monitor",
     "desc": "Offline end-to-end test: injects a fake glitch into a synthetic "
              "store and verifies crawl → detect → alert → store. Touches no "
              "live app.",
      "meta": "Writes only the scratch deals.demo.db · ~10 s · use as a "
               "health check",
     "cmd": [sys.executable, "-u", "run.py", "--demo"]},
    {"id": "qc_status", "label": "QC health probe", "service": False, "group": "demand",
     "desc": "One live probe per quick-commerce app to verify extraction "
             "still works: products found, stock states, store id and ETA.",
     "meta": "light live check · ~45 s per app",
     "cmd": [sys.executable, "-u", "run.py", "--qc-status"],
     "args": [
         {"flag": "--apps", "kind": "text", "label": "apps",
          "ph": "blinkit,zepto,instamart"},
     ]},
    {"id": "demand_once", "label": "Demand round", "service": False, "group": "demand",
     "desc": "A single full demand-probe sweep across watchlist stores, then "
             "stops — collects the same data as the Demand prober without "
             "looping forever.",
     "meta": "live crawl · ~70 s · lighter alternative to the prober",
     "cmd": [sys.executable, "-u", "run.py", "--demand", "--once"],
     "args": [
         {"flag": "--apps", "kind": "text", "label": "apps",
          "ph": "blinkit,zepto,instamart"},
         {"flag": "--store", "kind": "text", "label": "store id",
          "ph": "e.g. 34292"},
         {"flag": "--max-terms", "kind": "int", "label": "max terms/store",
          "ph": "default 5"},
     ]},
    {"id": "store_inventory", "label": "Store inventory", "service": False, "group": "demand",
     "desc": "Product-Space Intelligence capture (M1): targets ONE store (--app + "
             "--store required) and runs a full every-category sweep, writing the rich "
             "inventory_catalog (url + raw_json + collections) to inventory/<app>.db AND "
             "the operational snapshot to deals.db. Pass --lat/--lon to target any store, "
             "even one outside this machine's real location.",
     "meta": "LIVE crawl · ~20–60 min per store · run ALONE — concurrent crawls get "
             "rate-limited into empty results",
     "cmd": [sys.executable, "-u", "run.py", "--store-inventory"],
     "args": [
         {"flag": "--app", "kind": "text", "label": "app (required)",
          "ph": "blinkit|zepto|instamart"},
         {"flag": "--store", "kind": "text", "label": "store id (required)",
          "ph": "e.g. 34292"},
         {"flag": "--lat", "kind": "float", "label": "lat override",
          "ph": "e.g. 19.1364"},
         {"flag": "--lon", "kind": "float", "label": "lon override",
          "ph": "e.g. 72.8296"},
         {"flag": "--mirror-page-ms", "kind": "int", "label": "mirror page ms"},
         {"flag": "--tabs", "kind": "int", "label": "tabs"},
     ]},
    {"id": "map_locality", "label": "Map locality", "service": False, "group": "demand",
     "desc": "Discovers darkstores for the configured locality (Andheri West) "
             "anchor-by-anchor into deals.db and exports rotation-pool JSONs.",
     "meta": "LIVE crawl · ~2 min+ per app",
     "cmd": [sys.executable, "-u", "run.py", "--map-locality"],
     "args": [
         {"flag": "--apps", "kind": "text", "label": "apps",
          "ph": "blinkit,zepto,instamart"},
         {"flag": "--max-points", "kind": "int", "label": "max points"},
     ]},
    {"id": "build_watchlist", "label": "Build watchlist", "service": False, "group": "demand",
     "desc": "Builds per-store SKU probe sets from live category/search "
             "sweeps — the list of items the Demand prober then tracks for "
             "stock-outs.",
     "meta": "LIVE crawl · writes the watchlist table in deals.db",
     "cmd": [sys.executable, "-u", "run.py", "--build-watchlist"],
     "args": [
         {"flag": "--apps", "kind": "text", "label": "apps",
          "ph": "blinkit,zepto,instamart"},
         {"flag": "--store", "kind": "text", "label": "store id",
          "ph": "e.g. 34292"},
         {"flag": "--max-per-store", "kind": "int", "label": "max SKUs/store"},
         {"flag": "--max-queries", "kind": "int", "label": "max queries"},
         {"flag": "--categories", "kind": "int", "label": "categories",
          "ph": "overrides categories_per_store"},
         {"flag": "--catalog", "kind": "bool", "label": "catalog inventory",
          "ph": "full snapshot + new/delisted churn · ~20–60 min/store — "
                "run ONE store at a time"},
     ]},
    {"id": "demand_report", "label": "Demand report", "service": False, "group": "demand",
     "desc": "Prints the Demand Pressure Index ranking and hour×SKU onset "
             "heatmap summary computed from already-recorded data.",
     "meta": "NO crawling · safe anytime · --csv exports exports/dpi_*.csv",
     "cmd": [sys.executable, "-u", "run.py", "--demand-report"],
     "args": [
         {"flag": "--store", "kind": "text", "label": "store id",
          "ph": "e.g. 34292"},
         {"flag": "--csv", "kind": "bool", "label": "export CSV"},
     ]},
    {"id": "catalog_report", "label": "Catalog report", "service": False, "group": "demand",
     "desc": "Prints the catalog-inventory snapshot history and the "
             "new/delisted churn log (limited-time arrivals + discontinued "
             "products) computed from already-recorded data.",
     "meta": "NO crawling · safe anytime · pairs with Build watchlist ▸ "
             "catalog inventory",
     "cmd": [sys.executable, "-u", "run.py", "--catalog-report"],
     "args": [
         {"flag": "--store", "kind": "text", "label": "store id",
          "ph": "e.g. 34292"},
     ]},
    {"id": "purge_vouchers", "label": "Purge vouchers", "service": False, "group": "demand",
     "desc": "Wipes voucher/gift-card rows from Demand Radar tables "
             "(watchlist/stock_obs/oos_events); keeps a deals.db backup. "
             "--demand also auto-purges at startup.",
     "meta": "NO crawling · safe anytime · idempotent · dry-run lists "
             "without deleting",
     "cmd": [sys.executable, "-u", "run.py", "--purge-vouchers"],
     "args": [
         {"flag": "--dry-run", "kind": "bool", "label": "dry run"},
     ]},
]


# -- per-feature argument editing -------------------------------------------
# The Features panel POSTs {"args": {"--store": "34292", "--catalog": true}}.
# Security posture: EVERY flag must be declared in that feature's `args`
# spec — unknown flags are rejected (400), never forwarded — so the panel
# can never become an arbitrary-CLI runner. Values are plain argv tokens
# (no shell involved); run.py's _flag_value takes the FIRST occurrence of a
# flag, so an override REPLACES the base cmd's occurrence (_merge_feature_cmd).

_ARG_FLAG_RE = re.compile(r"^--[a-z0-9][a-z0-9-]*$")
_ARG_VAL_MAX = 120


def _validate_feature_args(f, args):
    """Return an error string for invalid panel args, or None when valid.

    `args` maps flag -> value; bool-kind flags accept True/False/"" (bare
    flag). int/float kinds are numeric-checked; text values are length- and
    control-char-checked. Only spec-declared flags pass.
    """
    if not isinstance(args, dict):
        return "args must be an object of {flag: value}"
    spec = {a["flag"]: a for a in f.get("args", [])}
    if len(args) > 12:
        return "too many args (max 12)"
    for flag, val in args.items():
        if not isinstance(flag, str) or not _ARG_FLAG_RE.match(flag):
            return f"bad flag {flag!r}"
        if flag not in spec:
            return f"flag {flag} is not offered by '{f['id']}'"
        kind = spec[flag].get("kind", "text")
        if kind == "bool":
            if val not in (None, "", True, False, 0, 1):
                return f"{flag} takes no value"
            continue
        s = "" if val is None else str(val).strip()
        if not s:
            return f"{flag} needs a value"
        if len(s) > _ARG_VAL_MAX or "\n" in s or "\x00" in s:
            return f"{flag} value invalid (length/control chars)"
        if kind == "int":
            try:
                if int(s) < 0:
                    return f"{flag} must be >= 0"
            except ValueError:
                return f"{flag} needs an integer"
        elif kind == "float":
            try:
                float(s)
            except ValueError:
                return f"{flag} needs a number"
    return None


def _merge_feature_cmd(f, args):
    """Build the final command vector: base cmd + validated args.

    Value flags append `flag value`; bool flags append the flag alone (only
    when truthy). A flag the panel overrides is REMOVED from the base vector
    first — run.py's _flag_value reads the FIRST occurrence, so base
    `--max-terms 5` would otherwise silently ignore the panel's value.
    """
    base = list(f["cmd"])
    extra = []
    for a in f.get("args", []):
        flag = a["flag"]
        if flag not in args:
            continue
        if a.get("kind", "text") == "bool":
            if args[flag] in (True, 1, "1", "true", "True", "on", "yes"):
                extra.append(flag)
            continue
        extra.extend([flag, str(args[flag]).strip()])
    overridden = {t for t in extra if t.startswith("--")} & \
                 {t for t in base if t.startswith("--")}
    out, i = [], 0
    while i < len(base):
        t = base[i]
        if t in overridden:
            if i + 1 < len(base) and not base[i + 1].startswith("--"):
                i += 2          # drop the base flag AND its value
            else:
                i += 1          # base bool flag
            continue
        out.append(t)
        i += 1
    return out + extra


class FeatureManager:
    """Start/stop repo features as child processes; capture their output.

    Each feature gets a ring buffer of stdout+stderr lines (children launch
    unbuffered via -u), a pid and exit code. Everything still running is
    terminated when the dashboard shuts down — never orphan crawlers.
    """

    LOG_LINES = 2000

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
                    "group": f.get("group", "demand"),
                    "service": f["service"],
                    "args": f.get("args", []),
                    "running": running,
                    "pid": st["proc"].pid if running else None,
                    "started_ts": st["started_ts"] if st else None,
                    "uptime_sec": round(time.time() - st["started_ts"]) if running else None,
                    "returncode": (st["returncode"] if st and not running
                                   else None),
                    "finished_ok": (st["returncode"] == 0) if st and not running else None,
                })
        return {"features": out}

    def log_tail(self, fid, limit=None):
        with self._lock:
            st = self.procs.get(fid)
            lines = list(st["log"]) if st else []
        running = bool(st and st["proc"].poll() is None)
        try:
            n = int(limit) if limit is not None else self.LOG_LINES
        except (TypeError, ValueError):
            n = self.LOG_LINES
        n = max(1, min(n, self.LOG_LINES))
        return {"id": fid, "running": running,
                "lines": lines[-n:],
                "total": len(lines),
                "returncode": st["returncode"] if st and not running else None}

    # -- control ----------------------------------------------------------
    def start(self, fid, extra_args=None):
        f = self.catalog.get(fid)
        if not f:
            return {"error": f"unknown feature '{fid}'"}, 404
        cmd = list(f["cmd"])
        if extra_args:
            err = _validate_feature_args(f, extra_args)
            if err:
                return {"error": err}, 400
            cmd = _merge_feature_cmd(f, extra_args)
        with self._lock:
            st = self.procs.get(fid)
            if st and st["proc"].poll() is None:
                return {"error": f"'{fid}' is already running (pid {st['proc'].pid})"}, 409
            try:
                proc = subprocess.Popen(
                    cmd, cwd=self.cwd,
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
    """All repo-root sqlite databases worth browsing (sidecars excluded).

    Also scans the `inventory/` subfolder (per-app inventory_<app>.db files) and
    reports those under an `inventory/...` display name so the /db browser can
    reach them. Display names are repo-relative paths (may contain a slash).
    """
    out = []

    def _scan(folder, prefix):
        if not os.path.isdir(folder):
            return
        for name in sorted(os.listdir(folder)):
            if not name.endswith(".db"):
                continue
            path = os.path.join(folder, name)
            if not os.path.isfile(path):
                continue
            disp = f"{prefix}{name}"
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
                "name": disp,
                "size_kb": round(os.path.getsize(path) / 1024, 1),
                "modified": time.strftime("%d %b %H:%M",
                                          time.localtime(os.path.getmtime(path))),
                "tables": [{"name": t, "rows": counts.get(t)} for t in tables]
                if not counts.get("_error") else [],
                "error": counts.get("_error"),
            })

    _scan(_ROOT, "")
    _scan(os.path.join(_ROOT, "inventory"), "inventory/")
    return {"databases": out}


def _table_preview(db_name, table, limit=30):
    """Recent rows of a table, read-only. Names validated against the DB's own
    schema before any interpolation. `db_name` is a repo-relative path that may
    contain a single subfolder (e.g. `inventory/inventory_blinkit.db`); traversal
    outside the repo root is rejected."""
    if not db_name.endswith(".db") or "\\" in db_name or ".." in db_name:
        return {"error": "bad database name"}, 400
    path = os.path.normpath(os.path.join(_ROOT, db_name))
    root_abs = os.path.abspath(_ROOT)
    if os.path.abspath(path) != root_abs and not os.path.abspath(path).startswith(
            root_abs + os.sep):
        return {"error": "bad database name"}, 400
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


# ---------------- working-area (locations) ----------------
# The tool's geography lives entirely in config.yaml: geo.corridor (glitch
# monitor stations), demand.locality (Demand Radar bbox + landmarks) and
# search.station (bot search anchor). These helpers let the dashboard edit
# those blocks SAFELY: generated blocks mimic the existing miniyaml-compatible
# style, every write is validated with BOTH loaders before it lands, and the
# previous file is backed up to /tmp.

_CFG_PATH = os.path.join(_ROOT, "config.yaml")

# AI report filenames are strict: ai_<kind>_<YYYY-MM-DD_HHMMSS>.md — the
# download route matches this shape so nothing else in exports/ is reachable.
_AI_REPORT_RE = re.compile(r"ai_[a-z]+_[0-9\-_]+\.md")
# Data Explorer export files: DPI CSVs + locality JSONs. Strictly no
# separators / dotfiles so only flat files directly inside exports/ are served.
_EXPORT_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\.(csv|json)$")


def _save_ai_report(kind, model, text):
    """Persist a full AI analysis to exports/<kind>_<stamp>.md so long outputs
    never live or die inside the dashboard panel. Returns the filename, or
    None if the write failed (a disk hiccup must never eat the answer)."""
    try:
        exp = os.path.join(_ROOT, "exports")
        os.makedirs(exp, exist_ok=True)
        fname = time.strftime(f"{kind}_%Y-%m-%d_%H%M%S.md")
        head = (f"# qcom-scraping AI report — {kind.removeprefix('ai_').replace('_', ' ')}\n\n"
                f"- generated: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
                f"- model: {model}\n\n---\n\n")
        with open(os.path.join(exp, fname), "w", encoding="utf-8") as f:
            f.write(head + (text or "").strip() + "\n")
        return fname
    except Exception:
        return None


def _yaml_check(text):
    """Validate YAML text with the same loaders run.py uses. Error str or None."""
    try:
        import yaml
        try:
            yaml.safe_load(text)
            return None
        except Exception as ex:
            return f"pyyaml: {str(ex)[:160]}"
    except ImportError:
        from . import miniyaml
        try:
            miniyaml.loads(text)
            return None
        except Exception as ex:
            return f"miniyaml: {str(ex)[:160]}"


def _cfg_parse():
    """Parse the live config.yaml. Returns (data, error)."""
    with open(_CFG_PATH, encoding="utf-8") as f:
        text = f.read()
    err = _yaml_check(text)
    if err:
        return None, err
    try:
        import yaml
        return yaml.safe_load(text), None
    except ImportError:
        from . import miniyaml
        return miniyaml.loads(text), None


def _block_span(lines, key, indent):
    """[start, end) line span of the `key:` block nested at exactly `indent`
    spaces. end = first later non-blank line whose indent <= key's."""
    pat = re.compile(r"^(\s*)" + re.escape(key) + r"\s*:\s*(#.*)?$")
    start = None
    for i, ln in enumerate(lines):
        m = pat.match(ln)
        if m and len(m.group(1)) == indent:
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        ln = lines[j]
        if not ln.strip():
            continue
        if len(ln) - len(ln.lstrip()) <= indent:
            end = j
            break
    return start, end


def _fmt(v):
    """Compact numeric formatting for generated YAML (trims trailing zeros)."""
    if isinstance(v, float):
        s = f"{v:.5f}".rstrip("0").rstrip(".")
        return s or "0"
    return str(v)


def _q(s):
    """Quote a string for double-quoted YAML flow style."""
    return '"' + str(s).replace("\\", "\\\\").replace('"', "'") + '"'


def _write_cfg(new_text):
    """Backup current file to /tmp, then atomically replace config.yaml."""
    import shutil
    backup = f"/tmp/config.yaml.bak-{int(time.time())}"
    shutil.copyfile(_CFG_PATH, backup)
    tmp = _CFG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(new_text)
    os.replace(tmp, _CFG_PATH)
    return backup


def _location_state():
    cfg, err = _cfg_parse()
    if err:
        return {"error": err}
    corr = [{"station": s.get("station"), "lat": s.get("lat"), "lon": s.get("lon")}
            for s in ((cfg.get("geo") or {}).get("corridor") or [])]
    loc = (cfg.get("demand") or {}).get("locality") or {}
    bbox = loc.get("bbox") or {}
    center = None
    try:
        center = [round((float(bbox["min_lat"]) + float(bbox["max_lat"])) / 2, 5),
                  round((float(bbox["min_lon"]) + float(bbox["max_lon"])) / 2, 5)]
    except Exception:
        pass
    return {
        "corridor": corr,
        "search_station": (cfg.get("search") or {}).get("station"),
        "locality": {
            "name": loc.get("name"),
            "bbox": bbox,
            "grid_step_m": loc.get("grid_step_m"),
            "landmarks": [l if isinstance(l, dict) else str(l) for l in (loc.get("landmarks") or [])],
            "center": center,
        },
    }


_IPLOC = {"ts": 0.0, "data": None}


def _ip_location_cached(ttl=600):
    if time.time() - _IPLOC["ts"] > ttl or _IPLOC["data"] is None:
        try:
            from .inventory import approx_location
            _IPLOC["data"] = approx_location(timeout=5)
        except Exception as ex:
            _IPLOC["data"] = {"error": str(ex)[:140]}
        _IPLOC["ts"] = time.time()
    return _IPLOC["data"]


def _location_presets():
    """Pickable points: repo landmark presets flattened + area centers +
    the live corridor stations (always relevant, zero invented geography)."""
    from .locality import PRESET_LANDMARKS
    out = []
    for area, marks in sorted(PRESET_LANDMARKS.items()):
        pts = list(marks.values())
        out.append({"name": f"{area.title()} (area center)",
                    "area": area.title(),
                    "lat": round(sum(p[0] for p in pts) / len(pts), 5),
                    "lon": round(sum(p[1] for p in pts) / len(pts), 5)})
        for nm, (la, lo) in sorted(marks.items()):
            out.append({"name": nm.title(), "area": area.title(),
                        "lat": la, "lon": lo})
    cfg, err = _cfg_parse()
    if not err:
        for s in ((cfg.get("geo") or {}).get("corridor") or []):
            out.append({"name": f"Corridor · {s.get('station')}",
                        "area": "Monitor corridor",
                        "lat": s.get("lat"), "lon": s.get("lon")})
    return {"presets": out}


def _write_locality(body):
    """Replace demand.locality with a square bbox around one point."""
    name = str(body.get("name") or "Custom area").strip()[:60] or "Custom area"
    try:
        lat = float(body["lat"]); lon = float(body["lon"])
        radius_km = float(body.get("radius_km", 3))
    except (KeyError, TypeError, ValueError):
        return {"error": "lat, lon, radius_km required"}, 400
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or not (0.2 <= radius_km <= 25):
        return {"error": "lat/lon/radius_km out of range"}, 400

    cfg, err = _cfg_parse()
    if err:
        return {"error": err}, 500
    old_grid = int(((cfg.get("demand") or {}).get("locality") or {}).get("grid_step_m", 700))

    with open(_CFG_PATH, encoding="utf-8") as f:
        lines = f.read().splitlines()
    span = _block_span(lines, "locality", 2)
    if not span:
        return {"error": "config.yaml has no 'demand.locality' block"}, 500
    s, e = span

    dlat = radius_km / 111.320
    dlon = radius_km / (111.320 * max(math.cos(math.radians(lat)), 0.01))
    block = [
        "  locality:",
        f"    # edited via dashboard {time.strftime('%d-%m %H:%M')}",
        f"    name: {_q(name)}",
        "    bbox:",
        f"      min_lat: {_fmt(lat - dlat)}",
        f"      max_lat: {_fmt(lat + dlat)}",
        f"      min_lon: {_fmt(lon - dlon)}",
        f"      max_lon: {_fmt(lon + dlon)}",
        f"    grid_step_m: {old_grid}",
        "    landmarks:",
        "      # explicit form — resolves exactly, no preset matching:",
        f"      - {{ name: {_q(name + ' center')}, lat: {_fmt(lat)}, lon: {_fmt(lon)} }}",
    ]
    new_lines = lines[:s] + block + lines[e:]
    new_text = "\n".join(new_lines).rstrip("\n") + "\n"
    if (verr := _yaml_check(new_text)):
        return {"error": f"generated config failed validation ({verr}) — nothing written"}, 500
    backup = _write_cfg(new_text)
    st = _location_state()
    st.update({"ok": True, "backup": backup,
               "note": "applies when you (re)start a feature from the Features panel"})
    return st, 200


def _write_corridor(body):
    stations_in = body.get("stations")
    if not isinstance(stations_in, list) or not (1 <= len(stations_in) <= 40):
        return {"error": "stations list (1..40) required"}, 400
    clean = []
    seen = set()
    for st in stations_in:
        nm = str(st.get("station") or "").strip()[:30]
        try:
            la = round(float(st["lat"]), 5); lo = round(float(st["lon"]), 5)
        except (KeyError, TypeError, ValueError):
            return {"error": "each station needs station/lat/lon"}, 400
        if not nm or not (-90 <= la <= 90 and -180 <= lo <= 180):
            return {"error": f"bad station row: {nm!r}"}, 400
        if nm.lower() in seen:
            return {"error": f"duplicate station name: {nm}"}, 400
        seen.add(nm.lower())
        clean.append((nm, la, lo))

    search_station = body.get("search_station")
    with open(_CFG_PATH, encoding="utf-8") as f:
        text = f.read()
    lines = text.splitlines()
    span = _block_span(lines, "corridor", 2)
    if not span:
        return {"error": "config.yaml has no 'geo.corridor' block"}, 500
    s, e = span
    w = max(len(nm) for nm, _, _ in clean)
    block = ["  corridor:"]
    block += [f"    - {{ station: {_q(nm)+',':<{w+3}} lat: {_fmt(la)}, lon: {_fmt(lo)} }}"
              for nm, la, lo in clean]

    new_lines = lines[:s] + block + lines[e:]
    new_text = "\n".join(new_lines)

    if search_station is not None:
        snm = str(search_station).strip()
        if snm and snm.lower() not in seen:
            return {"error": f"search anchor '{snm}' is not one of the stations"}, 400
        pat = re.compile(r'^(\s{2}station:\s*)"[^"]*"(,.*)?$')
        hits = [(i, pat.match(ln)) for i, ln in enumerate(new_lines)]
        hits = [(i, m) for i, m in hits if m]
        warn = None
        if snm:
            if len(hits) != 1:
                warn = "could not locate unique 'search.station' line — anchor unchanged"
            else:
                i, m = hits[0]
                tail = m.group(2) or ""
                comment = ""
                if "#" in tail:
                    comment = "  " + tail[tail.index("#"):]
                new_lines[i] = f'{m.group(1)}{_q(snm)}{comment}'.rstrip()

    new_text = "\n".join(new_lines).rstrip("\n") + "\n"
    if (verr := _yaml_check(new_text)):
        return {"error": f"generated config failed validation ({verr}) — nothing written"}, 500
    backup = _write_cfg(new_text)
    st = _location_state()
    st.update({"ok": True, "backup": backup, "warn": warn,
               "note": "applies when you (re)start a feature from the Features panel"})
    return st, 200


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
                elif self.path == "/location":
                    try:
                        st = _location_state()
                        st["ip_location"] = _ip_location_cached()
                        try:
                            # Darkstores recorded so far (glitch monitor +
                            # Demand Radar phases) for map pins. Read-only
                            # sqlite peek; failures must not break /location.
                            from .store import Store as S
                            dbs = S(dash.cfg.get("db", "deals.db")).darkstores()
                        except Exception:
                            dbs = []
                        st["darkstores"] = [
                            {"app": a, "store_id": sid, "label": lab,
                             "lat": la, "lon": lo, "eta_min": eta}
                            for (a, sid, lab, la, lo, eta) in dbs
                            if la is not None and lo is not None
                        ]
                        self._json(st)
                    except Exception as ex:
                        self._json({"error": str(ex)[:200]})
                elif self.path == "/location/presets":
                    try:
                        self._json(_location_presets())
                    except Exception as ex:
                        self._json({"error": str(ex)[:200], "presets": []})
                elif self.path == "/ai/status":
                    try:
                        cfg, err = _cfg_parse()
                        from .ai_assist import AiAssist, live_state
                        st = AiAssist(cfg if not err else {}).status()
                        st["live"] = live_state()
                        self._json(st)
                    except Exception as ex:
                        self._json({"available": False, "reason": str(ex)[:200]})
                elif self.path.split("?", 1)[0].startswith("/features/") and self.path.split("?", 1)[0].endswith("/log"):
                    # /features/<id>/log?limit=N (default: whole retained buffer)
                    from urllib.parse import urlparse as _lp, parse_qs as _lqs
                    _lparts = _lp(self.path)
                    fid = _lparts.path[len("/features/"):-len("/log")]
                    _lim = (_lqs(_lparts.query).get("limit") or [None])[0]
                    self._json(dash.features.log_tail(fid, limit=_lim))
                elif self.path == "/db":
                    try:
                        self._json(_sqlite_files())
                    except Exception as ex:
                        self._json({"error": str(ex)[:200], "databases": []})
                elif self.path.startswith("/db/"):
                    # /db/<name>/<table>?limit=N  (name may contain '/', e.g.
                    # inventory/inventory_blinkit.db — so everything between
                    # 'db' and the final segment is the database path)
                    from urllib.parse import urlparse, parse_qs, unquote
                    parts = urlparse(self.path)
                    # unquote BEFORE splitting: the panel encodeURIComponent()s
                    # the db name, so a subfolder slash arrives as %2F
                    seg = [s for s in unquote(parts.path).split("/") if s]  # ['db', *name, table]
                    if len(seg) < 3:
                        self._json({"error": "use /db/<name>/<table>"}, 400)
                        return
                    limit = (parse_qs(parts.query).get("limit") or [30])[0]
                    try:
                        limit = int(limit)
                    except ValueError:
                        limit = 30
                    db_name = "/".join(seg[1:-1])
                    table = seg[-1]
                    data, code = _table_preview(db_name, table, limit)
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
                elif self.path.startswith("/catalog"):
                    # Catalog-inventory churn (09-02): snapshots + new/delisted.
                    try:
                        from urllib.parse import urlparse, parse_qs
                        from .store import Store as S
                        qs = parse_qs(urlparse(self.path).query)
                        sid = (qs.get("store") or [None])[0]
                        st = S(dash.cfg.get("db", "deals.db"))
                        self._json({
                            "snapshots": st.catalog_snapshot_list(store_id=sid),
                            "new": st.catalog_event_list(kind="new", store_id=sid, limit=40),
                            "delisted": st.catalog_event_list(kind="delisted",
                                                              store_id=sid, limit=40),
                        })
                        st.close()
                    except Exception as ex:
                        self._json({"error": str(ex)[:200], "snapshots": []})
                elif self.path == "/qc":
                    # DB-backed platform health (no live probing here — use
                    # `run.py --qc-status` for that; it takes ~45 s/app).
                    try:
                        from .store import Store as S
                        st = S(dash.cfg.get("db", "deals.db"))
                        day_ago = time.time() - 86400
                        apps = []
                        for a in ("blinkit", "zepto", "instamart", "bigbasket", "jiomart"):
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
                elif self.path.split("?", 1)[0] == "/semsearch":
                    # Offline semantic search: lexical name lookup (?q=) or
                    # stored-vector expansion across all silos (?seed=). No
                    # embedding-model call -- pure sqlite + cosine.
                    from urllib.parse import urlparse, parse_qs
                    qs = parse_qs(urlparse(self.path).query)
                    db = dash.cfg.get("db", "deals.db")
                    try:
                        from .embed import similar_across_silos, lexical_seeds
                        seed = (qs.get("seed") or [None])[0]
                        q = (qs.get("q") or [None])[0]
                        limit = int((qs.get("limit") or ["12"])[0])
                        if seed:
                            self._json({"seed": seed,
                                         "panels": similar_across_silos(db, seed, limit=limit)})
                        elif q:
                            self._json({"query": q,
                                         "seeds": lexical_seeds(db, q, limit=max(limit * 2, 20))})
                        else:
                            self._json({"error": "pass ?q=<text> or ?seed=<name>"}, 400)
                    except Exception as ex:
                        self._json({"error": str(ex)[:200]}, 500)
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
                elif self.path == "/exports":
                    # Read-only listing of export files (DPI CSVs + locality
                    # JSONs) for the Data Explorer tab.
                    try:
                        exp = os.path.join(_ROOT, "exports")
                        files = []
                        for name in sorted(os.listdir(exp)):
                            if not _EXPORT_RE.fullmatch(name):
                                continue
                            try:
                                st = os.stat(os.path.join(exp, name))
                                files.append({
                                    "name": name,
                                    "size_kb": round(st.st_size / 1024, 1),
                                    "mtime": time.strftime("%Y-%m-%d %H:%M",
                                                           time.localtime(st.st_mtime)),
                                })
                            except OSError:
                                pass
                        self._json({"files": files})
                    except OSError as ex:
                        self._json({"error": str(ex)[:200], "files": []})
                elif self.path.startswith("/exports/"):
                    # Download one export file. Same strict-filename guard as
                    # /ai/report — only flat, allowlisted files in exports/.
                    name = self.path[len("/exports/"):]
                    fp = os.path.join(_ROOT, "exports", name)
                    if (not _EXPORT_RE.fullmatch(name) or ".." in name
                            or not os.path.isfile(fp)):
                        self._json({"error": "export not found"}, 404)
                        return
                    try:
                        with open(fp, "rb") as f:
                            data = f.read()
                    except OSError as ex:
                        self._json({"error": str(ex)[:200]}, 500)
                        return
                    ctype = "application/json" if name.endswith(".json") else "text/csv"
                    self.send_response(200)
                    self.send_header("Content-Type", ctype + "; charset=utf-8")
                    self.send_header("Content-Disposition",
                                     f'attachment; filename="{name}"')
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif self.path.startswith("/ai/report/"):
                    # Download an AI analysis saved by /ai/explain. Name is
                    # strictly patterned (no separators) so only reports
                    # inside exports/ can ever be served.
                    name = self.path[len("/ai/report/"):]
                    fp = os.path.join(_ROOT, "exports", name)
                    if not _AI_REPORT_RE.fullmatch(name) or not os.path.isfile(fp):
                        self._json({"error": "report not found"}, 404)
                        return
                    try:
                        with open(fp, "rb") as f:
                            data = f.read()
                    except OSError as ex:
                        self._json({"error": str(ex)[:200]}, 500)
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "text/markdown; charset=utf-8")
                    self.send_header("Content-Disposition",
                                     f'attachment; filename="{name}"')
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
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
                    args = payload.get("args") if isinstance(payload, dict) else None
                    data, code = dash.features.start(fid, extra_args=args)
                    self._json(data, code)

                elif self.path.startswith("/features/") and self.path.endswith("/stop"):
                    fid = self.path[len("/features/"):-len("/stop")]
                    data, code = dash.features.stop(fid)
                    self._json(data, code)

                elif self.path == "/location/locality":
                    try:
                        data, code = _write_locality(payload)
                        self._json(data, code)
                    except Exception as ex:
                        self._json({"error": str(ex)[:200]}, 500)

                elif self.path == "/location/corridor":
                    try:
                        data, code = _write_corridor(payload)
                        self._json(data, code)
                    except Exception as ex:
                        self._json({"error": str(ex)[:200]}, 500)

                elif self.path.startswith("/ai/"):
                    try:
                        cfg, err = _cfg_parse()
                        if err:
                            return self._json({"error": f"config.yaml: {err}"}, 500)
                        from .ai_assist import (AiAssist, explain_results,
                                                explain_followup, LAST_EXPLAIN,
                                                suggest_methodology,
                                                suggest_focus, apply_methodology,
                                                apply_focus)
                        ai = AiAssist(cfg)
                        if not ai.available and self.path != "/ai/status":
                            return self._json(
                                {"error": "AI assistance is off — " + ai.unavailable_reason() +
                                          ". Set AI_API_KEY in .env or point ai.base_url at a "
                                          "local Ollama, then retry."}, 400)
                        db = dash.cfg.get("db", "deals.db")
                        if self.path == "/ai/explain":
                            text = explain_results(ai, db)
                            fname = _save_ai_report("ai_explain", ai.model, text)
                            LAST_EXPLAIN["report"] = fname or ""
                            self._json({"text": text, "report": fname,
                                        "download": ("/ai/report/" + fname)
                                                    if fname else None})
                        elif self.path == "/ai/explain/followup":
                            # Follow-up about the analysis just shown. The
                            # client echoes its displayed text as a fallback
                            # for a dashboard restart (server state lost).
                            text = explain_followup(ai, db,
                                                    payload.get("question"),
                                                    payload.get("prior"))
                            self._json({"text": text,
                                        "report": LAST_EXPLAIN.get("report") or None})
                        elif self.path == "/ai/methodology":
                            self._json(suggest_methodology(ai, cfg, db))
                        elif self.path == "/ai/methodology/apply":
                            data, code = apply_methodology(cfg, payload.get("changes"))
                            self._json(data, code)
                        elif self.path == "/ai/focus":
                            self._json(suggest_focus(ai, cfg, db, payload.get("intent")))
                        elif self.path == "/ai/focus/apply":
                            data, code = apply_focus(cfg, payload.get("staple_queries"))
                            self._json(data, code)
                        else:
                            self._json({"error": "not found"}, 404)
                    except RuntimeError as ex:      # LLM/network errors → readable 502
                        self._json({"error": str(ex)[:300]}, 502)
                    except Exception as ex:
                        self._json({"error": str(ex)[:200]}, 500)

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
