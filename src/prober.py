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
   5. Voucher exclusion: gift cards / instant vouchers are NOT commodities —
      they are dropped from every sweep before observation or event handling
      (demand.exclude_vouchers, default on). Their code-pool "stock-outs"
      must never fabricate demand.

Temporal resolution note: stock-outs shorter than one probe interval are
invisible by construction — the heatmap's honesty depends on stating this.
"""
from __future__ import annotations

import datetime
import random
import time

from . import events
from .geo import Corridor
from .adapters.base import Adapter  # noqa: F401  (type docs)
from .locality import QC_APPS
from .orchestrator import _in_quiet
from .store import is_voucher_name


def parse_sweep_windows(raw):
    """`demand.sweep_windows` -> set of local hours (0..23) the prober may
    START a round in.

    Accepts a single "H" / "H-H" token, a list of them, or the literal
    "0-23" / empty. The historical signal (02–10 / 15–16 have no
    observations) is captured by restricting the loop to those windows; the
    default of all 24 hours leaves behaviour unchanged. Ranges are inclusive;
    wrap-around (e.g. "22-2") is not supported and clamps to nothing.
    """
    if raw is None or raw == "" or raw == "0-23" or raw == ["0-23"] \
            or (isinstance(raw, (list, tuple)) and not raw):
        return set(range(24))
    toks = raw if isinstance(raw, (list, tuple)) else [raw]
    hours = set()
    for tok in toks:
        tok = str(tok).strip()
        if "-" in tok:
            lo, hi = tok.split("-", 1)
            try:
                lo, hi = int(lo), int(hi)
            except ValueError:
                continue
            if lo <= hi:
                for h in range(max(0, lo), min(23, hi) + 1):
                    hours.add(h)
        else:
            try:
                h = int(tok)
            except ValueError:
                continue
            if 0 <= h <= 23:
                hours.add(h)
    return hours


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
        # Vouchers are not commodities — excluded from every demand surface
        # (AGENTS.md invariant; knob demand.exclude_vouchers, default on).
        self.exclude_vouchers = bool(dem.get("exclude_vouchers", True))
        # Silent-hour scheduling (09-02): hours (local) the loop may start
        # rounds in. Empty/all => every hour (no gating).
        self.sweep_hours = parse_sweep_windows(dem.get("sweep_windows", "0-23"))
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
        active_skus = [r[0] for r in self.db.watchlist_for_store(app, sid)
                       if not (self.exclude_vouchers and is_voucher_name(r[1]))]
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

        # Vouchers are excluded BEFORE anything else (even suspect detection):
        # gift cards / instant vouchers are not commodities, so their
        # code-pool "stock-outs" must not open oos_events, trip the mass-flip
        # soft-block guard or touch DPI. This covers SKUs that merely pass by
        # in search results (they never enter the watchlist). The old
        # voucher_type / restock-trigger tracking was removed with the rest
        # of that experiment (docs/tracking_expansion_vouchers.md is retired);
        # stock_obs.voucher_type survives only as an always-NULL legacy column.
        if self.exclude_vouchers:
            kept = [p for p in products if not is_voucher_name(p.get("name"))]
            n_excl = len(products) - len(kept)
            if n_excl:
                products = kept
                summary["vouchers_skipped"] = n_excl
                print(f"[prober] {app}/{sid}: excluded {n_excl} voucher/"
                      f"gift-card SKUs (not commodities)")

        suspect, why = self._is_suspect(app, sid, products)
        if suspect:
            summary["suspect"] = why
            print(f"[prober] {app}/{sid}: SUSPECT cycle — {why}; observations "
                  f"recorded, event machine frozen")

        # 1) observations (always, even suspect cycles — honest data)
        rows = []
        for p in products:
            rows.append({
                "sku_key": p["sku_key"],
                "in_stock": p.get("in_stock"),
                "price": p.get("price"),
                "mrp": p.get("mrp"),
                "source": self._source_of(p.get("collections")),
            })
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
            v_opened, v_closed = self._vanished_pass(app, sid, active_skus,
                                                     open_oos, seen)
            opened += v_opened
            closed += v_closed

        summary.update({"opened": opened, "closed": closed,
                        "terms": len(terms), "cats": self.categories})
        events.emit("sweep", f"{app}@{sid}", **{k: summary.get(k) for k in
                    ("products", "obs", "opened", "closed", "suspect")})
        print(f"[prober] {app}/{sid}: {len(products)} SKUs · obs={n} · "
              f"opened={opened} closed={closed} · eta={summary['eta_min']} · "
              f"{summary['sec']}s" + (f" · SUSPECT({why[:40]})" if suspect else ""))
        return summary

    def _vanished_pass(self, app, sid, active_skus, open_oos, seen):
        """One vanished-machine pass over the sighting set `seen`. Extracted
        from sweep_store so the offline self-test can drive it directly.
        Returns (opened, closed) event counts."""
        # 09-04 vanished guard: only SKUs the prober has ITSELF sighted
        # (any stock_obs row) may vanish. The watchlist is now an
        # exhaustive catalog census (~8-24k rows/store) while a light
        # sweep sights a few hundred SKUs/cycle — an unguarded restart
        # would fabricate thousands of 'vanished' events per store from
        # pure coverage gaps. A never-sighted row's delisting verdict
        # comes from the catalog_events snapshot diff, not the prober.
        sighted = self.db.sighted_skus(app, sid)
        opened = closed = 0
        for sku in (set(active_skus) | set(open_oos)) & sighted:
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
        return opened, closed

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

    def _next_window_start(self, t):
        """Smallest epoch >= t whose local hour is in self.sweep_hours.

        Used to hold the loop idle (no crawls, no rate-limit burn) outside the
        configured sweep_windows. A round already in flight always finishes.
        """
        dt = datetime.datetime.fromtimestamp(t)
        for _ in range(48):  # bound the scan (>= 2 days)
            if dt.hour in self.sweep_hours:
                return dt.timestamp()
            dt = (dt + datetime.timedelta(hours=1)).replace(
                minute=0, second=0, microsecond=0)
        return t  # safety: nothing matched (e.g. empty set) -> don't stall

    def loop(self, apps=None, store_filter=None):
        base = self.cfg.get("schedule", {})
        speedup = float(base.get("offpeak_speedup", 1.0))
        span = "all 24h" if len(self.sweep_hours) == 24 else \
            sorted(self.sweep_hours)
        print(f"[prober] entering loop · interval={self.interval}s · "
              f"debounce={self.debounce} · vanish_after={self.vanished_after} · "
              f"sweep_windows={span}")
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
            # Silent-hour gating: never START the next round outside sweep_windows.
            if len(self.sweep_hours) != 24:
                now = time.time()
                if datetime.datetime.fromtimestamp(now + wait).hour \
                        not in self.sweep_hours:
                    target = self._next_window_start(now + wait)
                    idle = max(30.0, target - now)
                    print(f"[prober] outside sweep_windows → idle until "
                          f"{datetime.datetime.fromtimestamp(target):%H:%M} "
                          f"(~{idle / 3600:.1f} h)")
                    wait = idle
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
    assert p.exclude_vouchers is True          # default knob = vouchers out
    assert is_voucher_name("Steam Instant Voucher")
    assert is_voucher_name("Blinkit Gift Card")
    assert is_voucher_name("Xbox Game Pass Ultimate Subscription Voucher")
    # bare brand names intentionally DON'T match — real voucher listings all
    # carry a "Voucher"/"Gift Card" token, and "steam"/"game pass" substrings
    # would false-positive on real commodities (garment steamer, …)
    assert not is_voucher_name("xbox game pass ultimate")
    assert not is_voucher_name("Garment Steamer")   # brand-token false positive
    assert not is_voucher_name("Amul Milk 1L")
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
    # sweep-window parsing (silent-hour scheduling, 09-02)
    assert parse_sweep_windows(None) == set(range(24))
    assert parse_sweep_windows("") == set(range(24))
    assert parse_sweep_windows("0-23") == set(range(24))
    assert parse_sweep_windows(["0-23"]) == set(range(24))
    assert parse_sweep_windows([]) == set(range(24))
    assert parse_sweep_windows(["2-10", "15-16"]) == \
        {*range(2, 11), 15, 16}
    assert parse_sweep_windows("7") == {7}
    assert parse_sweep_windows(["3", "18-20"]) == {3, 18, 19, 20}
    # wrap-around / garbage clamps to empty rather than erroring
    assert parse_sweep_windows("22-2") == set()
    assert parse_sweep_windows(["x", "1-3"]) == {1, 2, 3}

    # ---- vanished guard (09-04): absence only counts for SIGHTED SKUs ----
    # A watchlist census row the prober has never observed is a coverage gap,
    # never churn — otherwise restarting --demand over an 8-24k catalog
    # watchlist would fabricate thousands of 'vanished' events per store.
    class FakeDB:
        def __init__(self, sighted, open_oos=(), active=()):
            self._sighted, self._open_oos, self._active = sighted, open_oos, active
            self.opened = []      # (sku, kind) opened by the vanished machine
        def sighted_skus(self, app, sid):
            return self._sighted
        def open_events_for_store(self, app, sid, kind="oos"):
            return self._open_oos
        def open_event_state(self, app, sid, sku):
            return None
        def open_oos_event(self, app, sid, sku, started_at=None, kind="oos"):
            if (sku, kind) in self.opened:      # real Store dedupes open events
                return None
            self.opened.append((sku, kind))
        def close_oos_event(self, *a, **k):
            return False
        def last_obs_ts(self, app, sid, sku):
            return None
    def _vanish_once(fake, active, open_oos, seen, rounds=7):
        pro = StockProber({"demand": {}, "schedule": {}}, fake)
        pro.absence = {}
        for _ in range(rounds):   # > vanished_cycles consecutive misses
            pro._vanished_pass("blinkit", "s1", set(active), set(open_oos), set(seen))
        return fake.opened
    # never-sighted census rows are ignored even after many missed sweeps...
    opened = _vanish_once(FakeDB(sighted={"seen1"}, open_oos=[]),
                          active=["census1", "seen1"], open_oos=[], seen={"seen1"})
    assert opened == [], opened
    # ...a previously sighted SKU that stops appearing DOES vanish
    opened = _vanish_once(FakeDB(sighted={"gone1"}, open_oos=[]),
                          active=["gone1"], open_oos=[], seen=set())
    assert opened == [("gone1", "vanished")], opened
    print("vanished guard ok · census rows ignored · sighted SKU vanishes")
    print("offline sanity ok · sources:", srcs, "· suspect(canary-only-OOS):", sus,
          "· sweep_windows parse ok")