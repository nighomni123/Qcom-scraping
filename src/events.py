"""
events.py — tiny thread-safe event bus feeding the live dashboard.

Every interesting thing that happens anywhere in the process (crawler results,
glitch alerts, telegram queries, replies, cycle ticks) is emitted here. The
dashboard polls /status and renders the stream in real time.
"""
from __future__ import annotations

import time
import threading
from collections import deque

_LOCK = threading.Lock()
_EVENTS = deque(maxlen=600)
_COUNTERS = {"products_seen": 0, "cycles_done": 0, "queries": 0,
             "alerts": 0, "replies": 0}
_STARTED = time.time()


def emit(kind: str, text: str = "", **fields):
    ev = {"ts": time.time(), "kind": kind, "text": text}
    ev.update(fields)
    with _LOCK:
        _EVENTS.append(ev)


def bump(key: str, n: int = 1):
    with _LOCK:
        _COUNTERS[key] = _COUNTERS.get(key, 0) + n


def snapshot(limit=120):
    with _LOCK:
        events = list(_EVENTS)[-limit:]
        counters = dict(_COUNTERS)
        started = _STARTED
    return {
        "uptime_sec": round(time.time() - started),
        "started": started,
        "counters": counters,
        "events": [
            {
                "ts": e["ts"],
                "time": time.strftime("%H:%M:%S", time.localtime(e["ts"])),
                "kind": e["kind"],
                "text": e.get("text", ""),
                **{k: v for k, v in e.items()
                   if k not in ("ts", "kind", "text")},
            }
            for e in reversed(events)  # newest first
        ],
    }
