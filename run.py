#!/usr/bin/env python3
"""
run.py — Moneymaker v2 entrypoint.

Usage:
  python3 run.py            # continuous monitor loop
  python3 run.py --once    # single cycle (good for testing)
  python3 run.py --check   # validate config + adapter availability

Requires (for the browser-intercept QC adapters):  pip install playwright && playwright install chromium
Without it, the orchestrator still runs the e-commerce trackers + detection engine.
"""
from __future__ import annotations

import sys
import os

# allow `python3 run.py` from repo root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import yaml
    def load_cfg():
        with open("config.yaml") as f:
            return yaml.safe_load(f)
except ModuleNotFoundError:
    # stdlib-only fallback so the tool runs with zero external deps
    from src import miniyaml
    def load_cfg():
        return miniyaml.load("config.yaml")


from src.orchestrator import loop, build_adapters, run_cycle
from src.store import Store
from src.alert import Alert
from src.geo import Corridor
from src.honey import load_honey



def _flag_value(name, default=None):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def _flag_list(name):
    v = _flag_value(name)
    return [a.strip() for a in v.split(",") if a.strip()] if v else None


def _flag_int(name):
    v = _flag_value(name)
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _flag_float(name):
    v = _flag_value(name)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def main():
    cfg = load_cfg()
    # Reset the demo db BEFORE opening the Store connection (deleting the file
    # under an open sqlite handle causes "attempt to write a readonly database").
    if "--demo" in sys.argv:
        reset_demo_db(cfg)
    corridor = Corridor(cfg.get("geo", {}).get("corridor", []))
    honey = load_honey(cfg)
    store = Store(cfg.get("db", "deals.db"))
    alert = Alert(cfg)
    demo = "--demo" in sys.argv
    adapters = build_adapters(cfg, corridor, honey, demo=demo)

    if "--check" in sys.argv:
        print("config ok; stations:", len(corridor.anchors()))
        for a in adapters:
            label = "browser-intercept" if getattr(a, "APP_URL", None) else (
                "demo (simulated)" if a.name == "demo" else "http trackers")
            print(f"  adapter {a.name}: enabled ({label})")
        return

    if "--demo" in sys.argv:
        print("[demo] running the REAL pipeline (geo->adapter->detector->store->alert)")
        print("[demo] with a deliberately injected glitch: Blinkit / Andheri / Amul Milk 1L")
        print("[demo] normal ₹66 -> glitched ₹29\n")
        run_cycle(cfg, store, alert, adapters, corridor, honey)
        print_demo_report(store)
        return

    if "--map-locality" in sys.argv:
        from src.locality import LocalityMapper
        LocalityMapper(cfg, store).map_locality(apps=_flag_list("--apps"),
                                                max_points=(_flag_int("--max-points") or 10))
        return

    if "--build-watchlist" in sys.argv:
        from src.watchlist import WatchlistBuilder
        WatchlistBuilder(cfg, store).build(apps=_flag_list("--apps"),
                                           store_filter=_flag_value("--store"),
                                           max_per_store=_flag_int("--max-per-store"),
                                           max_queries=_flag_int("--max-queries"),
                                           catalog="--catalog" in sys.argv,
                                           categories_override=_flag_int("--categories"),
                                           mirror_page_ms=_flag_int("--mirror-page-ms"),
                                           tabs=_flag_int("--tabs"))
        return

    if "--store-inventory" in sys.argv:
        # Per-app darkstore inventories around the machine's real approximate
        # location (public-IP derived; --lat/--lon to override) into
        # inventory_<app>.db files — separate from deals.db.
        from src.inventory import run_inventory
        run_inventory(cfg, apps=_flag_list("--apps"),
                      lat=_flag_float("--lat"), lon=_flag_float("--lon"),
                      radius_m=_flag_int("--radius-m"),
                      max_points=(_flag_int("--max-points") or 10))
        return

    if "--purge-vouchers" in sys.argv:
        # One-shot maintenance: vouchers/gift cards are not commodities; wipe
        # their watchlist rows, stock_obs observations and oos_events.
        n = store.purge_vouchers(dry_run="--dry-run" in sys.argv)
        if not n:
            print("[purge-vouchers] no voucher rows found — nothing to do")
        else:
            tag = "would remove" if "--dry-run" in sys.argv else "removed"
            for t, c in n.items():
                print(f"[purge-vouchers] {tag} {c:>6} row(s) from {t}")
        return

    if "--demand" in sys.argv:
        from src.prober import StockProber
        # Skewed-data hygiene: purge any voucher/gift-card rows that predate
        # the exclusion invariant (idempotent no-op once clean).
        n = store.purge_vouchers()
        if n:
            print(f"[demand] purged stale voucher rows: "
                  + ", ".join(f"{t}={c}" for t, c in n.items()))
        prober = StockProber(cfg, store)
        if "--once" in sys.argv:
            rounds = prober.run_round(apps=_flag_list("--apps"),
                                      store_filter=_flag_value("--store"),
                                      max_terms=_flag_int("--max-terms"))
            print(f"[demand] single round done — {len(rounds)} store(s) swept")
            return
        prober.loop(apps=_flag_list("--apps"), store_filter=_flag_value("--store"))
        return

    if "--qc-status" in sys.argv:
        from src.locality import qc_health
        qc_health(cfg, apps=_flag_list("--apps"))
        return

    if "--demand-report" in sys.argv:
        from src.store import Store as _Store  # aliased: Store is used above
        from src import demand as D
        db = _Store(cfg.get("db", "deals.db"))
        store_filter = _flag_value("--store")
        summ = D.demand_summary(db)
        t = summ["totals"]
        print(f"[demand-report] {t['obs']:,} stock_obs over {t['obs_days']}d · "
              f"{t['open_oos']} open OOS · {t['vanished']} vanished")
        for s in summ["stores"]:
            print(f"  {s['app']}:{s['store_id']}  {s['label'] or '—'}  "
                  f"obs={s['obs']} skus={s['skus_seen']} active={s['active_skus']} "
                  f"oos={s['open_oos']} eta={s['eta_min']}")
        rows = D.dpi_table(db, store_id=store_filter)
        if rows:
            print(f"\n  {'DPI':>7}  {'events':>6}  {'OOS min':>8}  {'restock':>8}  SKU")
            for r in rows[:20]:
                print(f"  {r['dpi']:>7.2f}  {r['n_events']:>6}  {r['total_min']:>8.1f}  "
                      f"{str(r['mean_restock_min']):>8}  {r['name'][:48]}")
        else:
            print("\n  no OOS events yet — the prober loop is still filling history")
        hm = D.heatmap(db, store_id=store_filter)
        if hm["peak_hour"] is not None:
            print(f"\n  peak stock-out onset hour: {hm['peak_hour']:02d}:00 local")
        if "--csv" in sys.argv:
            print("  export:", D.export_csv(db))
        db.close()
        return

    if "--catalog-report" in sys.argv:
        # Catalog-inventory churn: snapshots + new/delisted per store.
        store_filter = _flag_value("--store")
        snaps = store.catalog_snapshot_list(store_id=store_filter)
        if not snaps:
            print("[catalog-report] no snapshots yet — run "
                  "`python3 run.py --build-watchlist --catalog` first")
            return
        import datetime
        def _fmt(ts):
            return datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
        print(f"[catalog-report] snapshots"
              + (f" for store {store_filter}" if store_filter else "") + ":")
        for ts, n_skus, n_new, n_del in snaps:
            print(f"  {_fmt(ts)}  skus={n_skus:<5} new={n_new:<4} delisted={n_del}")
        for kind, title in (("new", "NEW arrivals (latest first)"),
                            ("delisted", "DELISTED / discontinued (latest first)")):
            evs = store.catalog_event_list(kind=kind, store_id=store_filter)
            print(f"\n  {title}: {len(evs)} shown")
            for ts, app, sid, _k, name, price, detail in evs[:20]:
                when = _fmt(ts)
                cat = f"  [{detail}]" if detail and detail != "absent from full-catalog sweep" else ""
                print(f"    {when}  {app}:{sid[:12]:<12}  {str(name)[:44]:<46} "
                      f"₹{price if price is not None else '—'}{cat}")
        return

    if "--search" in sys.argv:
        i = sys.argv.index("--search")
        query = " ".join(sys.argv[i + 1:]) or "amul milk"
        from src.search import SearchEngine, format_reply
        engine = SearchEngine(cfg)
        print(f"[search] “{query}” — fanning out to all platforms simultaneously…\n")
        res = engine.search(query, source="cli")
        print(format_reply(res))
        # Proactive keyword watches: a CLI search can satisfy other chats' /watch.
        try:
            from src.tgbot import get_watcher
            n = get_watcher(cfg, store).check_products(res.get("results", []),
                                                       source="search")
            if n:
                print(f"[watch] {n} watch alert(s) pushed")
        except Exception as ex:
            print(f"[warn] watch check skipped: {str(ex)[:120]}")
        return

    if "--bot" in sys.argv or "--ui" in sys.argv:
        from src.tgbot import TGBot
        bot = TGBot(cfg)

        # Simultaneous operation: keep the monitor loop running in a background
        # thread while the Telegram bot polls in the foreground.
        if "--no-monitor" not in sys.argv:
            import threading
            t = threading.Thread(target=loop, args=(cfg,), daemon=True)
            t.start()
            mode = "dashboard + bot + monitor" if "--ui" in sys.argv else "monitor running in background; telegram bot live"
            print(f"[mode] {mode}")
        else:
            print("[bot-only] monitor disabled (--no-monitor)")

        if "--ui" in sys.argv:
            import threading, webbrowser
            from src.dashboard import Dashboard
            dash = Dashboard(cfg)
            threading.Timer(1.0, lambda: webbrowser.open(
                f"http://127.0.0.1:{dash.port}")).start()
            dash.serve_forever()   # blocks main thread; STOP button / Ctrl-C exits
            return
        bot.run()
        return

    if "--once" in sys.argv:
        run_cycle(cfg, store, alert, adapters, corridor, honey)
        print("single cycle complete.")
        return

    loop(cfg)


def reset_demo_db(cfg):
    import os
    p = cfg.get("db", "deals.db")
    if os.path.exists(p):
        os.remove(p)


def print_demo_report(store):
    rows = store.recent_alerts(limit=10)
    print(f"\n=== DEMO RESULT: {len(rows)} glitch(es) detected & alerted ===")
    for ts, app, store_id, reason, price, score, mrp in rows:
        import datetime
        t = datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S")
        ref = f" (MRP ₹{mrp:.0f})" if mrp else ""
        print(f"  [{t}] {app} @ {store_id}  ₹{price:.0f}{ref}  {reason}  (score {score})")
    print("\nFull alert log:")
    try:
        logf = "deals.log"
        if os.path.exists(logf):
            for line in open(logf):
                print("  " + line.rstrip())
    except Exception:
        pass



if __name__ == "__main__":
    main()
