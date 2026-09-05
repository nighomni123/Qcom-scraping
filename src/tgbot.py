"""
tgbot.py — Telegram front-end, stdlib only (no python-telegram-bot needed).

Long-polls getUpdates; every plain-text message is treated as a product query.
The search fans out to all platforms in parallel (src/search.py) and the ranked
cheapest-first result is sent back. /start and /help explain usage.

Proactive alerts: /watch <product> persists a keyword watch (`keyword_watches`
table); WatchPusher matches every crawl batch (monitor/demo loop) and every
--search / bot search against the active watches and pushes Telegram pings
under alert.rate_cap. /digest replies with today's cheapest find per category
plus the Demand Radar DPI top-5, read straight from deals.db (no crawling).

Env: TG_BOT_TOKEN in .env or environment. Get one from @BotFather.
"""
from __future__ import annotations

import json
import os
import time
import threading
import urllib.request
import urllib.parse

from .alert import _load_env
from .search import SearchEngine, format_reply, match_score
from .store import Store
from . import events

WATCH_COOLDOWN_SEC = 6 * 3600   # per-watch quiet window between pushes
MAX_WATCHES_PER_CHAT = 10       # sanity cap on /watch registrations per chat


class WatchPusher:
    """
    Proactive keyword-watch alerts (the engine behind /watch).

    check_products() matches result rows against active `keyword_watches` with
    search.match_score (the same token-overlap rule that ranks search results)
    and pushes a Telegram message to each watching chat. Rate discipline:

      * alert.rate_cap (config.yaml -> alert) — max pushes per hour process-wide
        (sliding window); 0 disables proactive watch pushes entirely.
      * WATCH_COOLDOWN_SEC — per-watch quiet window persisted in the DB
        (last_alerted_ts), so restarts never re-spam an already-pinged match.

    Sends are fire-and-forget: a failed push leaves the watch unmarked so the
    next batch retries, and callers mid-crawl/mid-search never see exceptions.
    """

    def __init__(self, cfg, store, sender=None):
        self.cfg = cfg
        self.store = store
        self.min_score = float(cfg.get("search", {}).get("min_match_score", 0.5))
        self.rate_cap = int(cfg.get("alert", {}).get("rate_cap", 10))
        # Semantic watch matching: a "cold drinks" watch should also catch
        # "Kinley Soda Lemontini". Falls back to token-only when embeddings
        # are unavailable — pushes never depend on the endpoint.
        from .embed import get_embedder
        self.emb = get_embedder(cfg, getattr(store, "path", "deals.db"))
        env = _load_env()
        self.token = env.get("TG_BOT_TOKEN") or os.environ.get("TG_BOT_TOKEN") or ""
        self._sent = []  # epoch times of successful pushes (hourly window)
        self._send_fn = sender if sender else self._tg_send

    def _tg_send(self, chat_id, text):
        """Push via the Bot API; returns True only when the call succeeded."""
        if not self.token:
            return False
        try:
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
            req = urllib.request.Request(url, data=data, method="POST")
            with urllib.request.urlopen(req, timeout=10):
                return True
        except Exception:
            return False

    def _cap_ok(self):
        if self.rate_cap <= 0:
            return False
        now = time.time()
        self._sent = [t for t in self._sent if now - t < 3600]
        return len(self._sent) < self.rate_cap

    def check_products(self, rows, source="crawl"):
        """
        rows: [{name, price|effective, platform|app, url}, ...] from any crawl
        or search batch. Returns the number of Telegram pushes actually sent.
        """
        if not rows or not self.token:
            return 0
        watches = self.store.active_watches()
        if not watches:
            return 0
        now = time.time()
        sent = 0
        for wid, chat_id, kw, last_ts in watches:
            if last_ts and now - last_ts < WATCH_COOLDOWN_SEC:
                continue
            hit = None
            # One batched embeddings call per keyword (query + all names),
            # not per row — a per-row probe would fire one request per
            # product. Falls back to token-only when embeddings are down.
            boost = self.emb.boost_many(kw, [r.get("name") for r in rows])
            for r in rows:  # first matching product of this batch is enough
                nm = (r.get("name") or "").strip()
                if nm:
                    s = max(match_score(kw, nm), boost.get(nm, 0.0))
                    if s >= self.min_score:
                        hit = (r, nm, s)
                        break
            if not hit:
                continue
            row, nm, s = hit
            if not self._cap_ok():
                return sent
            price = row.get("effective")
            if price is None:
                price = row.get("price")
            plat = str(row.get("platform") or row.get("app") or "?").upper()
            try:
                item = f"🔥 {plat} · ₹{float(price):.0f} — {nm[:70]}"
            except (TypeError, ValueError):
                item = f"🔥 {plat} — {nm[:70]}"
            msg = f"👀 *watch:* “{kw}” — matched ({source})\n{item}\n(match {s:.0%})"
            url = row.get("url")
            if url:
                msg += f"\n{url}"
            if self._send_fn(chat_id, msg):
                self.store.mark_watch_alerted(wid)
                self._sent.append(time.time())
                events.bump("watch_alerts")
                events.emit("watch_alert", f"“{kw}” ← {nm[:60]}",
                            keyword=kw, chat=chat_id, source=source)
                sent += 1
        return sent


_WATCHER = None
_WATCHER_LOCK = threading.Lock()


def get_watcher(cfg, store):
    """Process-wide WatchPusher so every entry point shares ONE hourly cap."""
    global _WATCHER
    with _WATCHER_LOCK:
        if _WATCHER is None:
            _WATCHER = WatchPusher(cfg, store)
        return _WATCHER


class TGBot:
    def __init__(self, cfg):
        env = _load_env()
        self.token = env.get("TG_BOT_TOKEN") or os.environ.get("TG_BOT_TOKEN") or ""
        self.cfg = cfg
        self.engine = SearchEngine(cfg)
        self.offset = 0
        self.base = f"https://api.telegram.org/bot{self.token}"
        self._busy = set()  # chat ids with a search in flight
        self.store = Store(cfg.get("db", "deals.db"))
        # Shared process-wide watcher: one hourly rate-cap window for all
        # entry points (bot searches, CLI --search, monitor crawl batches).
        self.watcher = get_watcher(cfg, self.store)

    def _api(self, method, params=None, timeout=35):
        url = f"{self.base}/{method}"
        data = urllib.parse.urlencode(params or {}).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())

    def _send(self, chat_id, text):
        try:
            self._api("sendMessage", {"chat_id": chat_id, "text": text,
                                      "parse_mode": "Markdown"}, timeout=15)
        except Exception:
            # Markdown can fail on odd chars; retry plain.
            try:
                self._api("sendMessage", {"chat_id": chat_id, "text": text}, timeout=15)
            except Exception:
                pass

    def _handle(self, chat_id, name, text):
        parts = text.split()
        cmd = parts[0].split("@")[0].lower() if parts else ""
        arg = " ".join(parts[1:])
        if cmd in ("/start", "/help"):
            self._send(chat_id,
                "🔎 Send me ANY product name and I'll find the cheapest one "
                "across Blinkit, Zepto, Instamart, Amazon & Flipkart — "
                "including delivery fees and applicable codes/offers.\n\n"
                "Example: amul milk 1l\n"
                "Example: lays chips\n"
                "Example: surf excel\n\n"
                "👀 /watch <product> — ping this chat whenever crawls or "
                "searches surface a match\n"
                "🗑 /unwatch <product> — stop watching\n"
                "📋 /digest — today's cheapest find per category + Demand "
                "Radar DPI top-5")
            return
        if cmd == "/watch":
            self._cmd_watch(chat_id, arg)
            return
        if cmd == "/unwatch":
            self._cmd_unwatch(chat_id, arg)
            return
        if cmd == "/digest":
            self._cmd_digest(chat_id, name)
            return
        if chat_id in self._busy:
            self._send(chat_id, "⏳ Still searching the previous item — one moment.")
            return
        self._busy.add(chat_id)
        events.bump("queries")
        events.emit("query", f"{name}: “{text}”", user=name)
        self._send(chat_id, f"🔎 Searching Blinkit · Zepto · Instamart · Amazon · Flipkart for “{text}”…")
        try:
            res = self.engine.search(text, source="telegram")
            top = res["results"][0] if res["results"] else None
            summary = (f"{top['platform']} ₹{top['effective']:.0f} — {top['name'][:40]}"
                       if top else "no matches")
            events.emit("reply", summary, query=text, top=top,
                        results=res.get("results", [])[:10],
                        search_id=res.get("search_id"),
                        elapsed=res.get("elapsed"))
            try:  # this search may satisfy other chats' watches too
                self.watcher.check_products(res.get("results", []),
                                            source="telegram")
            except Exception:
                pass
            self._send(chat_id, format_reply(res))
        except Exception as ex:
            self._send(chat_id, f"⚠️ Search failed: {str(ex)[:200]}")
        finally:
            self._busy.discard(chat_id)

    # ---- proactive-alert commands ----
    def _cmd_watch(self, chat_id, arg):
        arg = arg.strip()
        current = [k for k, _, _ in self.store.watches_for_chat(chat_id)]
        if not arg:
            if current:
                self._send(chat_id, "👀 Your watches:\n" +
                           "\n".join(f"• {k}" for k in current))
            else:
                self._send(chat_id,
                           "👀 No watches yet. Add one:\n/watch amul milk")
            return
        if len(current) >= MAX_WATCHES_PER_CHAT:
            self._send(chat_id, f"⚠️ Watch limit reached ({MAX_WATCHES_PER_CHAT}). "
                                "Use /unwatch first.")
            return
        if self.store.add_watch(chat_id, arg):
            events.bump("watches")
            events.emit("watch_add", f"chat {chat_id} → “{arg}”", keyword=arg)
            self._send(chat_id,
                       f"👀 Watching “{arg}”. I'll ping THIS chat whenever a "
                       "crawl or a search surfaces a matching product.")
        else:
            self._send(chat_id, f"👀 Already watching “{arg}”.")

    def _cmd_unwatch(self, chat_id, arg):
        arg = arg.strip()
        if arg and self.store.remove_watch(chat_id, arg):
            events.emit("watch_del", f"chat {chat_id} ✕ “{arg}”", keyword=arg)
            self._send(chat_id, f"🗑 Stopped watching “{arg}”.")
            return
        current = [k for k, _, _ in self.store.watches_for_chat(chat_id)]
        listing = "\n".join(f"• {k}" for k in current) if current else "(none)"
        head = (f"🤔 Not watching “{arg}”." if arg
                else "Usage: /unwatch <product>")
        self._send(chat_id, f"{head}\nYour watches:\n{listing}")

    def _cmd_digest(self, chat_id, name="there"):
        events.emit("query", f"{name}: /digest", user=name)
        lt = time.localtime()
        day_start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                                 0, 0, 0, lt.tm_wday, lt.tm_yday, -1))
        lines = [f"📋 *Digest* — {time.strftime('%d %b %Y')}", ""]
        cats = self.store.today_cheapest_by_category(day_start=day_start)
        lines.append("💸 *Today's cheapest find per category*")
        if cats:
            for c in cats:
                lines.append(f"• {c['category']}: ₹{c['effective']:.0f} — "
                             f"{(c['name'] or '?')[:44]} ({c['platform']})")
        else:
            lines.append("(no searches archived yet today)")
        lines.append("")
        lines.append("📈 *Demand Radar — highest-pressure SKUs (DPI)*")
        rows = []
        try:
            from . import demand as D  # pure sqlite rollups — no crawling
            rows = D.dpi_table(self.store, limit=5)
        except Exception:
            pass
        if rows:
            for i, r in enumerate(rows, 1):
                extra = (f", restock ≈{r['mean_restock_min']:.0f} min"
                         if r.get("mean_restock_min") else "")
                lines.append(f"{i}. {(r['name'] or r['sku_key'])[:44]} — "
                             f"DPI {r['dpi']:.1f} · "
                             f"{r['n_events']} stock-out(s){extra}")
        else:
            lines.append("(no stock-out history yet)")
        self._send(chat_id, "\n".join(lines)[:4000])

    def run(self):
        if not self.token:
            print("[tgbot] TG_BOT_TOKEN missing — put it in .env (see .env.example)")
            return
        me = self._api("getMe", timeout=15)
        username = me.get("result", {}).get("username")
        print(f"[tgbot] listening as @{username} — send it a product name")
        events.emit("bot_online", f"listening as @{username}")
        while True:
            try:
                data = self._api("getUpdates", {"timeout": 25, "offset": self.offset}, timeout=35)
                for upd in data.get("result", []):
                    self.offset = upd["update_id"] + 1
                    msg = upd.get("message") or upd.get("edited_message") or {}
                    chat_id = (msg.get("chat") or {}).get("id")
                    text = (msg.get("text") or "").strip()
                    if not chat_id or not text:
                        continue
                    name = (msg.get("from") or {}).get("first_name", "there")
                    threading.Thread(target=self._handle, args=(chat_id, name, text),
                                     daemon=True).start()
            except Exception as ex:
                print(f"[tgbot] poll error: {str(ex)[:120]}")
                time.sleep(3)
