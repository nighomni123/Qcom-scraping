"""
tgbot.py — Telegram front-end, stdlib only (no python-telegram-bot needed).

Long-polls getUpdates; every plain-text message is treated as a product query.
The search fans out to all platforms in parallel (src/search.py) and the ranked
cheapest-first result is sent back. /start and /help explain usage.

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
from .search import SearchEngine, format_reply
from . import events


class TGBot:
    def __init__(self, cfg):
        env = _load_env()
        self.token = env.get("TG_BOT_TOKEN") or os.environ.get("TG_BOT_TOKEN") or ""
        self.cfg = cfg
        self.engine = SearchEngine(cfg)
        self.offset = 0
        self.base = f"https://api.telegram.org/bot{self.token}"
        self._busy = set()  # chat ids with a search in flight

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
        if text.startswith("/start") or text.startswith("/help"):
            self._send(chat_id,
                "🔎 Send me ANY product name and I'll find the cheapest one "
                "across Blinkit, Zepto, Instamart, Amazon & Flipkart — "
                "including delivery fees and applicable codes/offers.\n\n"
                "Example: amul milk 1l\n"
                "Example: lays chips\n"
                "Example: surf excel")
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
            self._send(chat_id, format_reply(res))
        except Exception as ex:
            self._send(chat_id, f"⚠️ Search failed: {str(ex)[:200]}")
        finally:
            self._busy.discard(chat_id)

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
