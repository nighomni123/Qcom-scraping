"""
orchestrator.py — scheduler + cycle control.

Design choices that defeat naive polling detection:
  * Jittered cadence: each cycle sleeps a random fraction of cycle_seconds.
  * Off-peak speedup: crawls FASTER during quiet_hours (server rate limits are
    looser at night) — inverts the usual bot-burst pattern.
  * Per-station, per-app staggering so we never hit all stores at once.
  * Rotated UA / install-id / proxy per cycle (handled in adapters).
"""
from __future__ import annotations

import time
import random
import datetime
import math

from .geo import Corridor, resolve_store
from .store import Store
from .detect import evaluate
from .alert import Alert
from .honey import load_honey, honey_for_app
from . import events
from .adapters.blinkit import BlinkitAdapter
from .adapters.instamart import InstamartAdapter
from .adapters.zepto import ZeptoAdapter
from .adapters.trackers import TrackersAdapter
from .adapters.demo import DemoAdapter


def _in_quiet(cfg):
    q = cfg.get("schedule", {}).get("quiet_hours")
    if not q:
        return False
    now = datetime.datetime.now().strftime("%H:%M")
    s, e = q.get("start"), q.get("end")
    if s <= e:
        return s <= now <= e
    return now >= s or now <= e


def build_adapters(cfg, corridor, honey, demo=False):
    if demo:
        return [DemoAdapter(cfg, corridor, honey)]
    adapters = []
    a = cfg.get("adapters", {})
    if a.get("blinkit", {}).get("enabled"):
        adapters.append(BlinkitAdapter(cfg, corridor, honey))
    if a.get("instamart", {}).get("enabled"):
        adapters.append(InstamartAdapter(cfg, corridor, honey))
    if a.get("zepto", {}).get("enabled"):
        adapters.append(ZeptoAdapter(cfg, corridor, honey))
    if a.get("trackers", {}).get("enabled"):
        adapters.append(TrackersAdapter(cfg, corridor, honey))
    return adapters


def run_cycle(cfg, store, alert, adapters, corridor, honey):
    for station, lat, lon in corridor.anchors():
        store_id, store_label = resolve_store("multi", station, lat, lon)
        for ad in adapters:
            if ad.name == "trackers":
                products = ad.crawl()  # not geo-bound
                # trackers have no per-store concept; use a single label
                store_id, store_label = "trackers", "e-commerce"
            else:
                try:
                    t_crawl = time.time()
                    products = ad.crawl(station, lat, lon)
                    events.emit("crawl", f"{ad.name} @ {station}",
                                app=ad.name, station=station,
                                count=len(products or []),
                                sec=round(time.time() - t_crawl, 1),
                                samples=[{"name": p.get("name"), "price": p.get("price"),
                                          "mrp": p.get("mrp")}
                                         for p in (products or [])[:6]])
                except Exception as ex:
                    print(f"[warn] {ad.name}@{station} failed: {ex}")
                    continue
            app_label = getattr(ad, "honey_app", ad.name)
            app_honey = honey_for_app(app_label, honey)
            for p in products:
                price = p.get("price")
                mrp = p.get("mrp")
                sku = p.get("sku_key")
                events.bump("products_seen")
                # record history regardless
                store.record(ad.name, store_id, sku, p.get("name"), price, mrp, p.get("url", ""))
                is_glitch, score, reason = evaluate(
                    app_label, store_id, sku, price, mrp, cfg, store,
                    app_honey, name=p.get("name"),
                )
                if is_glitch and not store.last_alerted(ad.name, store_id, sku):
                    store.record_alert(ad.name, store_id, sku, reason, price, score)
                    alert.send(ad.name, store_label, p.get("name"), price, reason, score, p.get("url", ""))

            # Proactive keyword watches: match this crawl batch against
            # /watch registrations and push under alert.rate_cap. Best-effort:
            # a watcher problem must never take down a monitoring cycle.
            try:
                from .tgbot import get_watcher
                batch = [{"name": p.get("name"), "price": p.get("price"),
                          "platform": ad.name, "url": p.get("url", "")}
                         for p in (products or [])]
                get_watcher(cfg, store).check_products(
                    batch, source=f"{ad.name}@{station}")
            except Exception as ex:
                print(f"[warn] watch check failed: {str(ex)[:120]}")


def loop(cfg):
    store = Store(cfg.get("db", "deals.db"))
    alert = Alert(cfg)
    corridor = Corridor(cfg.get("geo", {}).get("corridor", []))
    honey = load_honey(cfg)
    adapters = build_adapters(cfg, corridor, honey)
    print(f"[ok] started; adapters={[a.name for a in adapters]} stations={len(corridor.anchors())}")

    base = cfg.get("schedule", {}).get("cycle_seconds", 300)
    speedup = cfg.get("schedule", {}).get("offpeak_speedup", 1.0)

    while True:
        t0 = time.time()
        events.emit("cycle", f"cycle #{events.snapshot()['counters']['cycles_done'] + 1} starting")
        try:
            run_cycle(cfg, store, alert, adapters, corridor, honey)
        except Exception as ex:
            print(f"[err] cycle: {ex}")
            events.emit("error", f"cycle failed: {str(ex)[:120]}")
        elapsed = time.time() - t0
        events.bump("cycles_done")
        # off-peak => crawl faster (smaller effective wait)
        factor = speedup if _in_quiet(cfg) else 1.0
        wait = max(0, base * factor - elapsed)
        wait = wait * random.uniform(0.7, 1.3)  # jitter
        print(f"[tick] cycle {elapsed:.1f}s; next in {wait:.0f}s (quiet={_in_quiet(cfg)})")
        events.emit("cycle_done", f"cycle took {elapsed:.0f}s · next in {wait:.0f}s",
                    elapsed=round(elapsed, 1), next_wait=round(wait))
        time.sleep(wait)
