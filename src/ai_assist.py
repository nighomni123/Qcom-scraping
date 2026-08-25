"""
ai_assist.py — OPTIONAL LLM assistant behind the dashboard's AI panel.

Three jobs, all strictly advisory-except-whitelisted-config:
  1. EXPLAIN      interpret Demand Radar outputs (DPI / events / heatmap)
  2. METHODOLOGY  propose probe-tuning changes for a tiny whitelist of
                  config.yaml knobs, which are applied ONLY through the same
                  validated writer the location editor uses
  3. FOCUS        turn a product-focus intent into concrete staple_queries
                  that --build-watchlist / --demand actually use

Provider: any OpenAI-compatible chat endpoint (stdlib urllib only).
Configure in config.yaml → ai: (enabled/base_url/model/max_tokens) and put
the key in .env as AI_API_KEY. A local Ollama works too:
    ai.base_url: "http://127.0.0.1:11434/v1"   (no API key needed)
Everything degrades gracefully: disabled/unconfigured ⇒ the dashboard shows
the panel greyed out with the reason, nothing calls the network.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

DEFAULT_BASE = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"

# The ONLY config keys the assistant may change, with safe bounds. Anything
# else in a suggestion is dropped before it can reach config.yaml.
INT_PARAMS = {
    "probe_interval_sec":     (60, 7200),
    "probe_terms_max":        (1, 60),
    "oos_debounce_snapshots": (1, 10),
    "vanished_cycles":        (1, 20),
    "categories_per_store":   (0, 20),
    "watchlist_max_per_store": (10, 2000),
}


def _root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class AiAssist:
    """Thin OpenAI-compatible client + configuration."""

    def __init__(self, cfg):
        ai = (cfg or {}).get("ai") or {}
        self.enabled = bool(ai.get("enabled", False))
        self.base = (str(ai.get("base_url") or "").strip() or DEFAULT_BASE).rstrip("/")
        self.model = str(ai.get("model") or DEFAULT_MODEL).strip()
        self.max_tokens = int(ai.get("max_tokens") or 700)
        try:
            from .alert import _load_env
            env = _load_env(os.path.join(_root(), ".env"))
        except Exception:
            env = {}
        self.key = (env.get("AI_API_KEY") or os.environ.get("AI_API_KEY") or "").strip()

    @property
    def available(self):
        return self.enabled and bool(self.model)

    def unavailable_reason(self):
        if not self.enabled:
            return "disabled — set ai.enabled: true in config.yaml"
        if not self.model:
            return "no model configured (config.yaml → ai.model)"
        return ""

    def status(self):
        local = any(h in self.base for h in ("localhost", "127.0.0.1"))
        return {
            "available": self.available,
            "enabled": self.enabled,
            "model": self.model,
            "base_url": self.base,
            "has_key": bool(self.key),
            "reason": self.unavailable_reason(),
            "hint": ("" if self.available else
                     "set AI_API_KEY in .env (or point ai.base_url at a local "
                     "Ollama: http://127.0.0.1:11434/v1)"),
            "needs_key": not local,
        }

    # -- raw chat ----------------------------------------------------------
    def chat(self, system, user, timeout=60):
        if not self.available:
            raise RuntimeError(self.unavailable_reason())
        url = f"{self.base}/chat/completions"
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": self.max_tokens,
            "temperature": 0.2,
        }
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        if self.key:
            req.add_header("Authorization", "Bearer " + self.key)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read().decode())
        except urllib.error.HTTPError as ex:
            detail = ""
            try:
                detail = ex.read().decode()[:220]
            except Exception:
                pass
            raise RuntimeError(f"AI endpoint HTTP {ex.code}: {detail}") from None
        except Exception as ex:
            raise RuntimeError(f"AI endpoint unreachable: {str(ex)[:180]}") from None
        # NB: .get("content", "") is NOT enough — some providers send
        # "content": null (content filters; reasoning models that spend the
        # whole budget on reasoning_content), which would make .strip() blow
        # up on None. Fall back to reasoning_content, then fail with WHY.
        msg = ((d.get("choices") or [{}])[0].get("message") or {})
        content = (msg.get("content") or msg.get("reasoning_content") or "")
        if not str(content).strip():
            fr = (d.get("choices") or [{}])[0].get("finish_reason")
            err = d.get("error") or {}
            hint = (f" (finish_reason={fr} — try raising ai.max_tokens)"
                    if fr == "length" else
                    (f" (finish_reason={fr})" if fr else ""))
            detail = f": {str(err)[:200]}" if err else ""
            raise RuntimeError(f"AI returned no content{hint}{detail}")
        return str(content).strip()


# ---------------- data digests (compact, token-friendly) ----------------

def _fmt_dpi(rows):
    out = ["sku | name | DPI | events | OOS-min | restock-min | last₹"]
    for r in rows[:8]:
        out.append(f"{r['sku_key'][:14]} | {str(r.get('name'))[:38]} | {r['dpi']} | "
                   f"{r['n_events']} | {r['total_min']} | {r.get('mean_restock_min')} | "
                   f"{r.get('last_price')}")
    return "\n".join(out)


def demand_digest(db_path):
    """Plain-text digest of everything the radar panels show."""
    from .store import Store
    from . import demand as D
    st = Store(db_path)
    try:
        dpi = D.dpi_table(st, limit=8)
        summ = D.demand_summary(st)
        hm = D.heatmap(st, top=6)
        eta = D.eta_curve(st)
    finally:
        st.close()
    L = ["== totals ==", json.dumps(summ.get("totals", {}))]
    L.append("== stores ==")
    for s in summ.get("stores", [])[:12]:
        L.append(f"{s['app']} {str(s['label'])[:26]} obs={s['obs']} skus={s['skus_seen']} "
                 f"active={s['active_skus']} open_oos={s['open_oos']} vanished={s['vanished']} eta={s['eta_min']}")
    L.append("== DPI top (demand pressure index = recency-weighted OOS min/day) ==")
    L += [_fmt_dpi(dpi)] if dpi else ["(no oos_events yet)"]
    L.append("== onset heatmap top cells (hour×sku) ==")
    L.append(json.dumps(hm)[:600] if hm else "(none)")
    L.append("== ETA curve by hour ==")
    L.append(json.dumps(eta)[:400])
    return "\n".join(L)


def methodology_digest(cfg, db_path):
    """Current tunables + probing health so suggestions stay grounded."""
    from .store import Store
    st = Store(db_path)
    q = lambda sql, *p: st.conn.execute(sql, p).fetchone()[0]
    try:
        h = {
            "obs_24h": q("SELECT COUNT(*) FROM stock_obs WHERE ts>?", time.time() - 86400),
            "null_pct_24h": round((q("SELECT COUNT(*) FROM stock_obs WHERE ts>? AND in_stock IS NULL",
                                     time.time() - 86400) *
                                   100.0) / max(1, q("SELECT COUNT(*) FROM stock_obs WHERE ts>?",
                                                     time.time() - 86400)), 1),
            "obs_7d": q("SELECT COUNT(*) FROM stock_obs WHERE ts>?", time.time() - 7 * 86400),
            "open_oos": q("SELECT COUNT(*) FROM oos_events WHERE ended_at IS NULL AND kind='oos'"),
            "open_vanished": q("SELECT COUNT(*) FROM oos_events WHERE ended_at IS NULL AND kind='vanished'"),
            "watch_active": q("SELECT COUNT(*) FROM watchlist WHERE active=1"),
            "stores": q("SELECT COUNT(*) FROM darkstores"),
            "distinct_skus_seen": q("SELECT COUNT(DISTINCT sku_key) FROM stock_obs"),
        }
    finally:
        st.close()
    dem = (cfg or {}).get("demand") or {}
    cur = {k: dem.get(k) for k in INT_PARAMS}
    cur["staple_queries"] = dem.get("staple_queries")
    L = ["== current demand-probing tunables ==",
         json.dumps(cur, default=str),
         "== probing health ==",
         json.dumps(h)]
    return "\n".join(L)


def focus_digest(cfg, db_path):
    from .store import Store
    st = Store(db_path)
    try:
        cats = st.conn.execute(
            "SELECT category, COUNT(*) c FROM price_obs GROUP BY category ORDER BY c DESC LIMIT 12").fetchall()
        sample = st.conn.execute(
            "SELECT DISTINCT name FROM price_obs LIMIT 15").fetchall()
    finally:
        st.close()
    cur = ((cfg or {}).get("demand") or {}).get("staple_queries")
    L = ["== current staple_queries ==", json.dumps(cur),
        "== categories already seen (count) ==",
        ", ".join(f"{c}({n})" for c, n in cats) or "(none)",
        "== sample product names ==",
        "; ".join(str(r[0])[:40] for r in sample) or "(none)"]
    return "\n".join(L)


# ---------------- prompts + task wrappers ----------------

_BASE_SYSTEM = (
    "You assist Moneymaker, a research tool that monitors Indian quick-commerce "
    "apps (Blinkit/Zepto/Swiggy Instamart) for price glitches and stock-out "
    "based demand signals. Rules: interpret ONLY the provided data; never "
    "invent numbers; remember stock-outs are a DEMAND PROXY, not sales; "
    "in_stock=NULL means unknown, never zero; be concise; plain bullets."
)


def explain_results(ai, db_path):
    digest = demand_digest(db_path)
    sys_p = (_BASE_SYSTEM +
             " Task: explain what the latest results mean for demand research — "
             "top pressure SKUs, temporal patterns, per-store anomalies, and any "
             "data-quality caveats. End with 'Next actions:' and 2-3 concrete ideas.")
    return ai.chat(sys_p, digest)


METHODOLOGY_RULES = (
    " Task: review the probing methodology. Propose AT MOST 5 adjustments to "
    "the listed integer tunables, grounded in the health numbers (e.g. high "
    "null% suggests slower probing; many vanished events suggests longer "
    "vanished_cycles; few observations suggests smaller probe_interval_sec). "
    "Respond STRICTLY as a JSON array, nothing else: "
    '[{"param":"<name from the tunables>","current":<num>,"suggested":<num>,'
    '"reason":"<one sentence>"}]'
)


def suggest_methodology(ai, cfg, db_path):
    digest = methodology_digest(cfg, db_path)
    raw = ai.chat(_BASE_SYSTEM + METHODOLOGY_RULES, digest)
    parsed = _extract_json(raw)
    if not isinstance(parsed, list):
        raise RuntimeError("assistant did not return a JSON array of suggestions")
    out = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        param = str(item.get("param", "")).strip()
        if param not in INT_PARAMS:
            continue                      # whitelist enforcement
        try:
            suggested = int(float(item["suggested"]))
            current = item.get("current")
        except (KeyError, TypeError, ValueError):
            continue
        lo, hi = INT_PARAMS[param]
        if not (lo <= suggested <= hi):
            continue                      # bounds enforcement
        out.append({"param": param, "current": current, "suggested": suggested,
                    "reason": str(item.get("reason", ""))[:240]})
    return {"suggestions": out[:6], "raw": raw[:2000]}


FOCUS_RULES = (
    " Task: choose WHAT PRODUCTS the demand prober should focus on. Convert "
    "the user's intent into concrete grocery search queries that work well on "
    "Indian quick-commerce apps (short, brand-or-category style, e.g. 'amul milk', "
    "'diapers size 3', 'cold drinks'). Respond STRICTLY as JSON, nothing else: "
    '{"staple_queries":["...", "..."], "categories":["..."], '
    '"rationale":"two sentences"}'
)


def suggest_focus(ai, cfg, db_path, intent):
    intent = str(intent or "").strip()[:400]
    if not intent:
        raise RuntimeError("describe the focus first, e.g. 'beverages and baby care'")
    digest = focus_digest(cfg, db_path)
    raw = ai.chat(_BASE_SYSTEM + FOCUS_RULES,
                  f"User intent: {intent}\n\nContext:\n{digest}")
    parsed = _extract_json(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError("assistant did not return a JSON object")
    queries = []
    for q in (parsed.get("staple_queries") or [])[:30]:
        q = re.sub(r"[^\w\s&'-]", "", str(q)).strip().lower()[:40]
        if q and q not in queries:
            queries.append(q)
    return {"staple_queries": queries,
            "categories": [str(c)[:30] for c in (parsed.get("categories") or [])[:8]],
            "rationale": str(parsed.get("rationale", ""))[:500],
            "raw": raw[:2000]}


# ---------------- applying changes (whitelisted, validated) ----------------

def _extract_json(text):
    """Best-effort: fenced ```json blocks, else first balanced object/array."""
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    candidates = [m.group(1)] if m else []
    candidates.append(text)
    for c in candidates:
        try:
            return json.loads(c.strip())
        except Exception:
            pass
    for op, cl in (("{", "}"), ("[", "]")):
        i = text.find(op)
        j = text.rfind(cl)
        if i != -1 and j > i:
            try:
                return json.loads(text[i:j + 1])
            except Exception:
                continue
    return None


def apply_methodology(cfg, changes):
    """changes: {param:int}. Whitelist + bounds enforced; writes config.yaml
    through the same validate-both-loaders + atomic-replace path as the
    location editor."""
    from .dashboard import _yaml_check, _write_cfg, _CFG_PATH  # lazy: no cycle
    if not isinstance(changes, dict) or not changes:
        return {"error": "no changes supplied"}, 400
    applied = {}
    for k, v in changes.items():
        if k not in INT_PARAMS:
            return {"error": f"'{k}' is not an adjustable parameter"}, 400
        lo, hi = INT_PARAMS[k]
        try:
            v = int(float(v))
        except (TypeError, ValueError):
            return {"error": f"{k}: not a number"}, 400
        if not (lo <= v <= hi):
            return {"error": f"{k}: {v} outside safe range {lo}..{hi}"}, 400
        applied[k] = v

    with open(_CFG_PATH, encoding="utf-8") as f:
        lines = f.read().splitlines()

    # locate demand: section span
    ds = de = None
    for i, ln in enumerate(lines):
        if ln.startswith("demand:"):
            ds = i
            break
    if ds is None:
        return {"error": "no demand: section in config.yaml"}, 500
    de = len(lines)
    for j in range(ds + 1, len(lines)):
        if lines[j].strip() and not lines[j].startswith((" ", "\t")):
            de = j
            break

    changed_lines = 0
    for key, val in applied.items():
        pat = re.compile(r"^(\s{2})(" + re.escape(key) + r"\s*:\s*)([^#]*?)(\s*#.*)?$")
        done = False
        for i in range(ds + 1, de):
            m = pat.match(lines[i])
            if m:
                lines[i] = f"{m.group(1)}{m.group(2)}{val}{m.group(4) or ''}"
                done = True
                changed_lines += 1
                break
        if not done:
            return {"error": f"key '{key}' not found under demand: — nothing written"}, 500

    new_text = "\n".join(lines).rstrip("\n") + "\n"
    if (verr := _yaml_check(new_text)):
        return {"error": f"generated config failed validation ({verr})"}, 500
    backup = _write_cfg(new_text)
    return {"ok": True, "applied": applied, "backup": backup,
            "note": "applies when you (re)start the Demand prober / watchlist builder"}, 200


def apply_focus(cfg, staple_queries):
    from .dashboard import _yaml_check, _write_cfg, _CFG_PATH
    if not isinstance(staple_queries, list) or not staple_queries:
        return {"error": "staple_queries list required"}, 400
    clean = []
    for q in staple_queries[:30]:
        q = re.sub(r"[^\w\s&'-]", "", str(q)).strip().lower()[:40]
        if q and q not in clean:
            clean.append(q)
    if not clean:
        return {"error": "no usable queries after cleaning"}, 400

    with open(_CFG_PATH, encoding="utf-8") as f:
        lines = f.read().splitlines()
    pat = re.compile(r"^(\s{2})(staple_queries\s*:\s*)(.*?)(\s*#.*)?$")
    done = False
    for i, ln in enumerate(lines):
        m = pat.match(ln)
        if m:
            flow = "[" + ", ".join(f'"{q}"' for q in clean) + "]"
            lines[i] = f"{m.group(1)}{m.group(2)}{flow}{m.group(4) or ''}"
            done = True
            break
    if not done:
        return {"error": "staple_queries key not found under demand:"}, 500
    new_text = "\n".join(lines).rstrip("\n") + "\n"
    if (verr := _yaml_check(new_text)):
        return {"error": f"generated config failed validation ({verr})"}, 500
    backup = _write_cfg(new_text)
    return {"ok": True, "applied": clean, "backup": backup,
            "note": "used by --build-watchlist / --demand on their next start"}, 200