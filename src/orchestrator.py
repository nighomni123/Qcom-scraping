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
from .adapters.bigbasket import BigbasketAdapter
from .adapters.jiomart import JiomartAdapter
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
    if a.get("bigbasket", {}).get("enabled"):
        adapters.append(BigbasketAdapter(cfg, corridor, honey))
    if a.get("jiomart", {}).get("enabled"):
        adapters.append(JiomartAdapter(cfg, corridor, honey))
    if a.get("trackers", {}).get("enabled"):
        adapters.append(TrackersAdapter(cfg, corridor, honey))
    return adapters


class WafFailover:
    """Blinkit-as-fallback when Zepto/Instamart are WAF-gated.

    Each app accrues 'strikes' on consecutive empty/errored crawls. Crossing
    ``empty_strikes_to_gate`` gates the app for ``gate_cooldown_cycles`` — we
    then SKIP it (hammering a WAF-blocked app deepens the ban / rate-limit) and
    instead give Blinkit extra per-station passes so coverage doesn't crater.
    Apps in ``protected_apps`` (default: blinkit) are never auto-gated, since
    they ARE the fallback. This operationalizes the reverse-engineering finding
    that Blinkit has no AWS WAF and no request signature, so it is the resilient
    crawl source of the three QC apps.
    """

    def __init__(self, cfg):
        fo = (cfg.get("schedule", {}) or {}).get("waf_failover", {}) or {}
        self.enabled = bool(fo.get("enabled", True))
        self.empty_strikes_to_gate = int(fo.get("empty_strikes_to_gate", 3))
        self.gate_cooldown_cycles = int(fo.get("gate_cooldown_cycles", 4))
        self.blinkit_priority_extra = int(fo.get("blinkit_priority_extra", 1))
        self.protected = set(fo.get("protected_apps", ["blinkit"]))
        self.cycle = 0
        self.strikes = {}        # app -> consecutive empty crawls
        self.gated_until = {}    # app -> cycle index until which gated

    def tick(self):
        self.cycle += 1

    def is_gated(self, app):
        if not self.enabled or app in self.protected:
            return False
        return self.gated_until.get(app, 0) > self.cycle

    def record(self, app, n_products):
        if not self.enabled or app in self.protected:
            return
        if n_products and n_products > 0:
            self.strikes[app] = 0
            self.gated_until.pop(app, None)
        else:
            self.strikes[app] = self.strikes.get(app, 0) + 1
            if self.strikes[app] >= self.empty_strikes_to_gate:
                self.gated_until[app] = self.cycle + self.gate_cooldown_cycles
                events.emit("failover", f"{app} gated after {self.strikes[app]} empty crawls; leaning on blinkit",
                            app=app, fkind="gate")

    def gated_apps(self, apps):
        return [a for a in apps if self.is_gated(a)]

    def blinkit_boost(self, apps):
        if "blinkit" not in apps:
            return 0
        others = [a for a in apps if a != "blinkit" and self.is_gated(a)]
        return self.blinkit_priority_extra if others else 0


def _crawl_and_process(ad, station, lat, lon, store, alert, honey, cfg,
                       failover=None, store_id=None, store_label=None):
    """Crawl one adapter at one station, process products, update failover.

    Returns product count (0 if skipped due to gating or empty). Trackers are
    not geo-bound (store_id/label fixed to 'trackers'). All the per-product
    recording / glitch detection / keyword-watch logic lives here so both the
    main loop and the Blinkit failover boost share it.
    """
    if failover and failover.is_gated(ad.name):
        events.emit("failover", f"skip {ad.name}@{station} (gated); leaning on blinkit",
                    app=ad.name, station=station, fkind="skip")
        return 0
    products = []
    if ad.name == "trackers":
        try:
            products = ad.crawl()
        except Exception as ex:
            print(f"[warn] trackers failed: {ex}")
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
            products = []
    n = len(products or [])

    # record + glitch detection
    app_label = getattr(ad, "honey_app", ad.name)
    app_honey = honey_for_app(app_label, honey)
    for p in products:
        price = p.get("price")
        mrp = p.get("mrp")
        sku = p.get("sku_key")
        events.bump("products_seen")
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

    if failover:
        failover.record(ad.name, n)
    return n


def run_cycle(cfg, store, alert, adapters, corridor, honey, failover=None):
    for station, lat, lon in corridor.anchors():
        store_id, store_label = resolve_store("multi", station, lat, lon)
        for ad in adapters:
            if ad.name == "trackers":
                _crawl_and_process(ad, station, lat, lon, store, alert, honey, cfg,
                                   failover, store_id="trackers", store_label="e-commerce")
            else:
                _crawl_and_process(ad, station, lat, lon, store, alert, honey, cfg,
                                   failover, store_id=store_id, store_label=store_label)
    # WAF failover: lean on blinkit when other apps are gated
    if failover:
        boost = failover.blinkit_boost([a.name for a in adapters])
        if boost > 0:
            blink = next((a for a in adapters if a.name == "blinkit"), None)
            if blink:
                gated = failover.gated_apps([a.name for a in adapters])
                events.emit("failover",
                            f"blinkit priority x{boost} — {len(gated)} app(s) gated: {', '.join(gated)}",
                            fkind="boost", blinkit_passes=boost * len(corridor.anchors()),
                            gated=gated)
                for _ in range(boost):
                    for station, lat, lon in corridor.anchors():
                        sid, sl = resolve_store("multi", station, lat, lon)
                        _crawl_and_process(blink, station, lat, lon, store, alert, honey,
                                           cfg, failover, store_id=sid, store_label=sl)


def loop(cfg):
    store = Store(cfg.get("db", "deals.db"))
    alert = Alert(cfg)
    corridor = Corridor(cfg.get("geo", {}).get("corridor", []))
    honey = load_honey(cfg)
    adapters = build_adapters(cfg, corridor, honey)
    failover = WafFailover(cfg)
    print(f"[ok] started; adapters={[a.name for a in adapters]} stations={len(corridor.anchors())}")

    base = cfg.get("schedule", {}).get("cycle_seconds", 300)
    speedup = cfg.get("schedule", {}).get("offpeak_speedup", 1.0)

    while True:
        failover.tick()
        t0 = time.time()
        events.emit("cycle", f"cycle #{events.snapshot()['counters']['cycles_done'] + 1} starting")
        try:
            run_cycle(cfg, store, alert, adapters, corridor, honey, failover=failover)
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
