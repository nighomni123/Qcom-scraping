"""
prober.py — Demand Radar phase 3: stock-observation loop + OOS event machine.

Per (app, store) cycle:
  1. ONE browser session deep sweep: home harvest -> category click-through ->
     the store's own watchlist search terms (most-covering first). Same signed
     traffic as every other phase; one chromium launch per store per cycle.
  2. Every SKU sighting is written as a stock_obs snapshot. A crawl failure or
     unparseable node is in_stock=NULL — NULL NEVER becomes 0, because a
     scraper error must not fabricate a stock-out (debounce rule #1).
  3. State machine -> oos_events:
       - 'oos' opens only after >= oos_debounce_snapshots consecutive 0-reads;
         nulls/absences pause the streak without resetting it.
       - any 1-read closes the open event (restock) and clears the streak.
       - 'vanished': an ACTIVE watchlist SKU unseen for >= vanished_cycles
         SUCCESSFUL sweeps opens an open-ended vanished event (delisting is
         not a stock-out, but it must not stay invisible either). The same
         absence rule also RECONCILES stale 'oos' events on SKUs outside the
         active set (e.g. search-passed items that fell out of coverage):
         the oos event is closed at the LAST OBSERVED reading — durations
         never fabricate unseen time — and re-opened honestly as 'vanished'.
  4. Health guards (canaries): configured canary queries must return at least
     one in-stock item, and a mass in-stock->OOS flip within one cycle is the
     classic soft-block signature. Suspect cycles still RECORD observations
     (marked honestly) but the event machine is FROZEN for that cycle.

Temporal resolution note: stock-outs shorter than one probe interval are
invisible by construction — the heatmap's honesty depends on stating this.
"""
from __future__ import annotations

import random
import time

from . import events
from .geo import Corridor
from .adapters.base import Adapter  # noqa: F401  (type docs)
from .locality import QC_APPS
from .orchestrator import _in_quiet


class StockProber:
    def __init__(self, cfg, db):
        self.cfg = cfg
        self.db = db
        dem = cfg.get("demand", {}) or {}
        self.interval = int(dem.get("probe_interval_sec", 900))
        self.categories = int(dem.get("categories_per_store", 6))
        self.terms_max = int(dem.get("probe_terms_max", 20))
        self.debounce = max(1, int(dem.get("oos_debounce_snapshots", 2)))
        self.vanished_after = max(1, int(dem.get("vanished_cycles", 4)))
        self.canary_queries = list(dem.get("stock_canary_queries") or ["amul milk"])
        self.flip_min = int(dem.get("suspect_flip_min", 10))
        self.flip_pct = float(dem.get("suspect_flip_pct", 50))
        # in-memory state (prober-lifetime)
        self.streaks = {}     # (app,sid,sku) -> {count, first_ts}
        self.absence = {}     # (app,sid,sku) -> consecutive missed sweeps
        self.last_state = {}  # (app,sid,sku) -> bool (last definite read)

    # -- helpers -----------------------------------------------------------
    def _make_adapter(self, app):
        corridor = Corridor(self.cfg.get("geo", {}).get("corridor", []) or [])
        return QC_APPS[app](self.cfg, corridor, [])

    def _source_of(self, collections):
        cols = [str(c) for c in (collections or [])]
        if any(c.startswith("q:") for c in cols):
            return "search"
        if any(c != "home" for c in cols):
            return "collection"
        return "home"

    def _recover_open_events(self, app, sid, active_skus):
        """
        Restart-proof state: rebuild streaks from recorded observations so a
        prober restart doesn't reset a half-counted stock-out, and already-open
        events get closed by the next restock instead of duplicating.
        """
        for sku in active_skus:
            key = (app, sid, sku)
            if key in self.streaks:
                continue
            cnt, ts0 = self.db.trailing_oos_streak(app, sid, sku)
            ev = self.db.open_event_state(app, sid, sku)
            if ev and ev[0] == "oos":
                self.streaks[key] = {"count": max(cnt, self.debounce),
                                     "first_ts": ev[1] or ts0 or time.time()}
            elif cnt > 0:
                self.streaks[key] = {"count": cnt, "first_ts": ts0 or time.time()}

    def _is_suspect(self, app, sid, products):
        """
        Soft-block heuristics. Returns (suspect: bool, why: str).
          * canary queries must surface >=1 in-stock product each
          * mass in-stock->OOS flip inside one cycle is suspicious
        """
        by_term = {}
        for p in products:
            for c in (p.get("collections") or []):
                if str(c).startswith("q:"):
                    by_term.setdefault(str(c)[2:], []).append(p)
        for term in self.canary_queries:
            hits = by_term.get(term, [])
            if hits and not any(p.get("in_stock") for p in hits):
                return True, f"canary “{term}” returned only OOS items"
        flips, prev_seen = 0, 0
        for p in products:
            key_gen = (app, sid, p["sku_key"])
            prev = self.last_state.get(key_gen)
            if prev is True:
                prev_seen += 1
                if p.get("in_stock") is False:
                    flips += 1
        if prev_seen >= self.flip_min and flips >= self.flip_min \
                and flips / max(prev_seen, 1) * 100.0 >= self.flip_pct:
            return True, f"{flips}/{prev_seen} previously-in-stock SKUs flipped OOS in one cycle"
        return False, ""

    # -- core --------------------------------------------------------------
    def sweep_store(self, app, sid, lat, lon, max_terms=None):
        """One probe cycle for one store. Returns dict summary."""
        terms = self.db.watchlist_terms(app, sid)[:max_terms or self.terms_max]
        active_skus = [r[0] for r in self.db.watchlist_for_store(app, sid)]
        if not active_skus:
            print(f"[prober] {app}/{sid}: empty active watchlist — run --build-watchlist")
            return {"skipped": True}
        self._recover_open_events(app, sid, active_skus)

        adapter = self._make_adapter(app)
        t0 = time.time()
        products, meta = adapter.deep_sweep(sid, lat, lon,
                                            categories=self.categories, terms=terms)
        ok = not meta.get("error") or bool(products)
        summary = {"app": app, "store": sid, "products": len(products),
                   "eta_min": meta.get("eta_min"), "sec": round(time.time() - t0, 1)}

        if not products:
            print(f"[prober] {app}/{sid}: crawl failed ({meta.get('error')}) "
                  f"— recording nothing (null ≠ OOS)")
            summary["failed"] = True
            return summary

        suspect, why = self._is_suspect(app, sid, products)
        if suspect:
            summary["suspect"] = why
            print(f"[prober] {app}/{sid}: SUSPECT cycle — {why}; observations "
                  f"recorded, event machine frozen")

        # 1) observations (always, even suspect cycles — honest data)
        rows = [{
            "sku_key": p["sku_key"],
            "in_stock": p.get("in_stock"),
            "price": p.get("price"),
            "mrp": p.get("mrp"),
            "source": self._source_of(p.get("collections")),
        } for p in products]
        n = self.db.record_stock_obs(app, sid, rows, eta_min=meta.get("eta_min"))
        summary["obs"] = n
        seen = {r["sku_key"] for r in rows}

        # 2) event machine (frozen on suspect cycles)
        opened = closed = 0
        if not suspect:
            now = time.time()
            for r in rows:
                key = (app, sid, r["sku_key"])
                st = self.streaks.get(key, {"count": 0, "first_ts": now})
                if r["in_stock"] is False:
                    if st["count"] == 0:
                        st["first_ts"] = now
                    st["count"] += 1
                    open_ev = self.db.open_event_state(app, sid, r["sku_key"])
                    if open_ev and open_ev[0] == "vanished":
                        self.db.close_oos_event(app, sid, r["sku_key"], snapshots=st["count"],
                                                kinds=("vanished",), ended_at=st["first_ts"])
                    if st["count"] >= self.debounce and not (
                            open_ev and open_ev[0] == "oos"):
                        self.db.open_oos_event(app, sid, r["sku_key"],
                                               started_at=st["first_ts"], kind="oos")
                        opened += 1
                    self.streaks[key] = st
                elif r["in_stock"] is True:
                    if self.db.close_oos_event(app, sid, r["sku_key"],
                                               snapshots=max(st["count"], 1)):
                        closed += 1
                    self.streaks[key] = {"count": 0, "first_ts": now}
                    self.absence[key] = 0
                # in_stock None -> leave streak untouched (pause, not reset)
                if r["in_stock"] is not None:
                    self.last_state[key] = r["in_stock"]

            # 3) vanished detection (successful sweeps only). Covers the
            #    ACTIVE watchlist set PLUS every SKU with an open 'oos'
            #    event: apps hide OOS items from listings, so a SKU that
            #    stops being sighted for vanished_cycles sweeps is delisting
            #    masquerading as infinite OOS — left alone its event never
            #    closes and DPI inflates with unseen time (observed 08-30:
            #    search-surfaced SKUs outside the watchlist held open oos
            #    events for 5 days). Close it at the LAST OBSERVED reading
            #    (durations never fabricate evidence) and re-open honestly
            #    as 'vanished'.
            open_oos = {sku for sku, _ in self.db.open_events_for_store(app, sid, "oos")}
            for sku in set(active_skus) | open_oos:
                key = (app, sid, sku)
                if sku in seen:
                    self.absence[key] = 0
                    continue
                miss = self.absence.get(key, 0) + 1
                self.absence[key] = miss
                if miss < self.vanished_after:
                    continue
                ev = self.db.open_event_state(app, sid, sku)
                if ev and ev[0] == "oos":
                    last = self.db.last_obs_ts(app, sid, sku) or time.time()
                    self.db.close_oos_event(app, sid, sku, snapshots=miss,
                                            kinds=("oos",), ended_at=last)
                    self.db.open_oos_event(app, sid, sku, started_at=last,
                                           kind="vanished")
                    opened += 1
                    closed += 1
                    print(f"[prober] {app}/{sid}: {sku} oos-event stale after "
                          f"{miss} sweeps — closed at last evidence, "
                          f"logged vanished")
                elif not ev:
                    self.db.open_oos_event(app, sid, sku, kind="vanished")
                    opened += 1
                    print(f"[prober] {app}/{sid}: {sku} VANISHED from listings "
                          f"({miss} sweeps) — logged as vanished")

        summary.update({"opened": opened, "closed": closed,
                        "terms": len(terms), "cats": self.categories})
        events.emit("sweep", f"{app}@{sid}", **{k: summary.get(k) for k in
                    ("products", "obs", "opened", "closed", "suspect")})
        print(f"[prober] {app}/{sid}: {len(products)} SKUs · obs={n} · "
              f"opened={opened} closed={closed} · eta={summary['eta_min']} · "
              f"{summary['sec']}s" + (f" · SUSPECT({why[:40]})" if suspect else ""))
        return summary

    def run_round(self, apps=None, store_filter=None, max_terms=None):
        want_apps = {a.strip().lower() for a in (apps or []) if a.strip()}
        stores = [s for s in self.db.darkstores()
                  if (not want_apps or s[0] in want_apps)
                  and (not store_filter or s[1] == store_filter)]
        if not stores:
            print("[prober] no darkstores — run --map-locality + --build-watchlist first")
            return []
        print(f"[prober] round over {len(stores)} store(s)")
        out = []
        for app, sid, label, lat, lon, eta in stores:
            try:
                out.append(self.sweep_store(app, sid, lat, lon, max_terms=max_terms))
            except KeyboardInterrupt:
                raise
            except Exception as ex:
                print(f"[prober] error {app}/{sid}: {str(ex)[:140]}")
            time.sleep(3 + random.random() * 4)   # politeness between stores
        return out

    def loop(self, apps=None, store_filter=None):
        base = self.cfg.get("schedule", {})
        speedup = float(base.get("offpeak_speedup", 1.0))
        print(f"[prober] entering loop · interval={self.interval}s · "
              f"debounce={self.debounce} · vanish_after={self.vanished_after}")
        while True:
            t0 = time.time()
            try:
                self.run_round(apps=apps, store_filter=store_filter)
            except KeyboardInterrupt:
                print("\n[prober] interrupted — stopping cleanly")
                return
            except Exception as ex:
                print(f"[prober] round failed: {str(ex)[:140]}")
            elapsed = time.time() - t0
            factor = speedup if _in_quiet(self.cfg) else 1.0
            wait = max(30.0, self.interval * factor - elapsed)
            wait *= random.uniform(0.85, 1.15)      # jitter
            print(f"[prober] next round in {wait / 60:.1f} min "
                  f"(quiet={_in_quiet(self.cfg)})")
            try:
                time.sleep(wait)
            except KeyboardInterrupt:
                print("\n[prober] interrupted — stopping cleanly")
                return


if __name__ == "__main__":
    # offline sanity: pure logic, no network/db
    p = StockProber({"demand": {}, "schedule": {}}, None)
    prods = [
        {"sku_key": "a", "in_stock": True, "collections": ["home"]},
        {"sku_key": "b", "in_stock": False, "collections": ["q:milk"]},
        {"sku_key": "c", "in_stock": None, "collections": ["Shampoo"]},
    ]
    srcs = [p._source_of(x["collections"]) for x in prods]
    assert srcs == ["home", "search", "collection"], srcs
    bad = [{"sku_key": "x", "in_stock": False, "collections": ["q:amul milk"]}]
    p.last_state = {}
    sus, why = p._is_suspect("blinkit", "s", bad)
    print("offline sanity ok · sources:", srcs, "· suspect(canary-only-OOS):", sus)
