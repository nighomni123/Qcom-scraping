"""
alert.py — fan-out: desktop notification, optional Telegram push, and a log file.

Designed to be safe when pieces are missing (no tg token => skip; no macOS =>
skip desktop gracefully).
"""
from __future__ import annotations

import os
import sys
import time
import subprocess
import datetime

from . import events


def _load_env(path=".env"):
    env = {}
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


class Alert:
    def __init__(self, cfg):
        self.cfg = cfg.get("alert", {})
        self.log_file = self.cfg.get("log_file", "deals.log")
        self.max_per_hour = self.cfg.get("max_alerts_per_hour", 30)
        self._times = []
        self.env = _load_env()

    def _ratelimit_ok(self):
        now = time.time()
        self._times = [t for t in self._times if now - t < 3600]
        if len(self._times) >= self.max_per_hour:
            return False
        self._times.append(now)
        return True

    def _desktop(self, title, msg):
        if not self.cfg.get("desktop"):
            return
        if sys.platform != "darwin":
            return
        try:
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{msg}" with title "{title}" sound name "Glass"'],
                check=False, timeout=5,
            )
        except Exception:
            pass

    def _telegram(self, text):
        tg = self.cfg.get("telegram", {})
        if not tg.get("enabled"):
            return
        token = self.env.get("TG_BOT_TOKEN") or os.environ.get("TG_BOT_TOKEN")
        chat = self.env.get("TG_CHAT_ID") or os.environ.get("TG_CHAT_ID")
        if not token or not chat:
            return
        try:
            import urllib.request, urllib.parse
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
            req = urllib.request.Request(url, data=data, method="POST")
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass

    def _log(self, line):
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(self.log_file, "a") as f:
                f.write(f"[{ts}] {line}\n")
        except Exception:
            pass

    def send(self, app, store_label, name, price, reason, score, url="",
             mrp=None, usual=None):
        """Fan out one glitch alert.

        mrp   — catalog MRP/usual from the crawl payload (may be None)
        usual — median of this (store,sku)'s recent prices BEFORE this
                observation (may be None); the honest "was" price when the
                catalog carries no MRP.
        """
        if not self._ratelimit_ok():
            return
        # Reference "was" price: prefer MRP, fall back to recent-store usual.
        # The live feed + modal fall back per-field so partial data still shows.
        was = mrp if mrp and mrp > 0 else usual
        title = f"🔥 {app} · {store_label}"
        msg = f"₹{price:.0f}"
        if was and was > 0 and was > price:
            msg += f" (was ₹{was:.0f})"
        msg += f" — {name} | {reason} (score {score})"
        # Desktop keeps the one-liner; Telegram gets a structured breakdown.
        self._desktop(title, msg)
        tg = f"{title}\n₹{price:.0f}"
        if mrp and mrp > 0 and mrp > price:
            tg += f" · MRP ₹{mrp:.0f}"
        if usual and usual > 0 and usual > price:
            tg += f" · usual ₹{usual:.0f}"
        tg += f"\n{name} | {reason} (score {score})\n{url}".rstrip()
        self._telegram(tg)
        # Log line keeps machine-parseable order: price, mrp, usual.
        log_msg = (f"₹{price:.0f} | mrp ₹{mrp:.0f} | usual ₹{usual:.0f}"
                   if mrp and usual
                   else f"₹{price:.0f}" + (f" | mrp ₹{mrp:.0f}" if mrp else "")
                   + (f" | usual ₹{usual:.0f}" if usual else ""))
        self._log(f"{title} | {log_msg} — {name} | {reason} (score {score}) | {url}")
        events.bump("alerts")
        events.emit("alert", f"{name} ₹{price:.0f}", platform=app,
                    store=store_label, reason=reason, url=url,
                    price=price, mrp=mrp, usual=usual)
