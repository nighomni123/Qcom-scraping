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

    def send(self, app, store_label, name, price, reason, score, url=""):
        if not self._ratelimit_ok():
            return
        title = f"🔥 {app} · {store_label}"
        msg = f"₹{price:.0f} — {name} | {reason} (score {score})"
        self._desktop(title, msg)
        self._telegram(f"{title}\n{msg}\n{url}".strip())
        self._log(f"{title} | {msg} | {url}")
        events.bump("alerts")
        events.emit("alert", f"{name} ₹{price:.0f}", platform=app,
                    store=store_label, reason=reason, url=url)
