#!/usr/bin/env python3
"""
run.py — qcom-scraping v2 entrypoint.

Usage:
  python3 run.py            # continuous monitor loop
  python3 run.py --monitor  # same monitor loop (explicit; pairs with --ui/--bot)
  python3 run.py --once    # single cycle (good for testing)
  python3 run.py --check   # validate config + adapter availability
  python3 run.py --ui      # dashboard ONLY — starts no loops; add --bot
                           # and/or --monitor to also run them alongside
  python3 run.py --bot     # telegram bot + monitor together (--no-monitor
                           # for bot only)

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
    # --demo runs the pipeline against a SCRATCH database — never the
    # production one. (09-04: --demo used to wipe whatever cfg["db"] pointed
    # at — including the live deals.db from the dashboard's one-click Demo
    # card — with a backup that nobody watched for. The demo runs on a clean
    # slate by design, so it only ever needs its own throwaway file.)
    if "--demo" in sys.argv:
        cfg = {**cfg, "db": "deals.demo.db"}
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
        # Single-store, every-category capture (Product-Space Intelligence M1):
        # --app + --store REQUIRED; --lat/--lon override the store's resolved
        # location. Writes the rich inventory_catalog (url + raw_json + collections)
        # to the per-app inventory DB and the operational snapshot to deals.db.
        from src.inventory import run_inventory
        app = _flag_value("--app")
        store_id = _flag_value("--store")
        if not app or not store_id:
            print("usage: python3 run.py --store-inventory --app <app> --store <store_id> "
                  "[--lat L --lon L] [--mirror-page-ms N] [--tabs N]")
            return
        run_inventory(cfg, app, store_id,
                      lat=_flag_float("--lat"), lon=_flag_float("--lon"),
                      mirror_page_ms=_flag_int("--mirror-page-ms"),
                      tabs=_flag_int("--tabs"))
        return

    if "--product-fields" in sys.argv:
        # M1 Phase 1: grocery name parser self-test / accuracy report.
        from src.product_fields import _run_selftest, sample_report
        if "--report" in sys.argv:
            i = sys.argv.index("--report")
            rest = sys.argv[i + 1:]
            sample_n, db = 200, cfg.get("db", "deals.db")
            for j, a in enumerate(rest):
                if a == "--sample" and j + 1 < len(rest):
                    try:
                        sample_n = int(rest[j + 1])
                    except ValueError:
                        pass
                elif a == "--db" and j + 1 < len(rest):
                    db = rest[j + 1]
            sample_report(db, sample_n)
        else:
            if not _run_selftest():
                import sys as _s
                _s.exit(1)
        return

    if "--product-space" in sys.argv:
        # M1 Phase 2: normalized product-space union (read-only). Joins the per-app
        # inventory DBs (rich capture) with deals.db catalog_snapshots; excludes
        # vouchers; resolves cross-app product groups via entity resolution.
        from src.product_space import load_product_space
        rows = load_product_space(apps=_flag_list("--apps"),
                                  attach_embeddings=("--embeddings" in sys.argv))
        if "--csv" in sys.argv:
            import csv, os
            out = "exports/product_space.csv"
            os.makedirs("exports", exist_ok=True)
            with open(out, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["product_group_id", "group_method", "group_confidence",
                            "app", "store_id", "sku_key", "name", "brand",
                            "pack_value", "pack_unit", "is_multipack", "variant",
                            "unit_base", "unit_price", "price", "mrp", "in_stock",
                            "category", "collections", "url", "source"])
                for r in rows:
                    w.writerow([r["product_group_id"], r["group_method"], r["group_confidence"],
                                r["app"], r["store_id"], r["sku_key"], r["name"], r["brand"],
                                r["pack_value"], r["pack_unit"], r["is_multipack"], r["variant"],
                                r["unit_base"], r["unit_price"], r["price"], r["mrp"],
                                r["in_stock"], r["category"], r["collections"], r["url"], r["source"]])
            print(f"[product-space] wrote {len(rows)} rows -> {out}")
        else:
            from collections import Counter
            print(f"[product-space] {len(rows)} normalized records · "
                  f"{len({r['product_group_id'] for r in rows})} product groups "
                  f"(vouchers excluded)")
            print("  apps:", dict(Counter(r["app"] for r in rows)))
            print("  top categories:", Counter(r["category"] for r in rows).most_common(6))
        return

    if "--product-vectors" in sys.argv:
        # M2 Phase 3B/3C: grocery-shaped attribute + commercial vectors per
        # product_group_id. Read-only over the union layer + Demand Radar rollup.
        from src.product_vectors import build_product_vectors
        from src.product_space import load_product_space
        import json
        rows = load_product_space(apps=_flag_list("--apps"))
        vecs = build_product_vectors(rows, db=store)
        if "--csv" in sys.argv:
            import csv, os
            out = "exports/product_vectors.csv"
            os.makedirs("exports", exist_ok=True)
            with open(out, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["product_group_id", "rep_name", "category", "member_count",
                            "present_apps", "unit_price", "pack_value",
                            "attr_unitprice_z", "attr_pack_z", "attr_multipack",
                            "attr_catdepth", "dpi", "store_count", "app_count",
                            "days_since_first_seen", "is_currently_active",
                            "recent_churn_flag", "flags"])
                for gid, v in vecs.items():
                    a = v["attribute_vector"]; c = v["commercial_features"]
                    w.writerow([gid, v["rep_name"], v["category"], v["member_count"],
                                ",".join(v["present_apps"]), v.get("unit_price"),
                                v.get("pack_value"), a[0], a[1], a[2], a[3],
                                c["dpi"], c["store_count"], c["app_count"],
                                c["days_since_first_seen"], c["is_currently_active"],
                                c["recent_churn_flag"], json.dumps(v["flags"])])
            print(f"[product-vectors] wrote {len(vecs)} groups -> {out}")
        else:
            multi = sum(1 for v in vecs.values()
                        if v["commercial_features"]["app_count"] > 1)
            print(f"[product-vectors] {len(vecs)} product groups vectorized "
                  f"({multi} cross-app)")
        return

    if "--detect-assortment-gaps" in sys.argv:
        # M3 Phase 6: cross-app assortment gaps from the union layer (coverage
        # signal only -- never a demand claim).
        from src.assortment_gaps import detect_assortment_gaps
        from src.product_space import load_product_space
        rows = load_product_space(apps=_flag_list("--apps"))
        gaps = detect_assortment_gaps(rows)
        if "--csv" in sys.argv:
            import csv, os
            out = "exports/assortment_gaps.csv"
            os.makedirs("exports", exist_ok=True)
            with open(out, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["product_group_id", "rep_name", "present_apps",
                            "missing_apps", "member_count", "store_count",
                            "category", "confidence"])
                for g in gaps:
                    w.writerow([g["product_group_id"], g["rep_name"],
                                ",".join(g["present_apps"]),
                                ",".join(g["missing_apps"]), g["member_count"],
                                g["store_count"], g["category"], g["confidence"]])
            print(f"[assortment-gaps] wrote {len(gaps)} candidates -> {out}")
        else:
            print(f"[assortment-gaps] {len(gaps)} cross-app assortment gaps")
            for g in gaps[:20]:
                print(f"  {g['rep_name'][:44]:<46} present="
                      f"{','.join(g['present_apps'])} missing="
                      f"{','.join(g['missing_apps'])} conf={g['confidence']}")
        return

    if "--product-density" in sys.argv:
        # M4 Phase 5: per-category local density + kNN distance with coverage
        # guards (insufficient_coverage / stale_coverage). No gap is ever emitted
        # from a guarded category -- this is the plan's core anti-false-positive.
        from src.density import compute_density, sparse_candidates
        from src.product_space import load_product_space
        min_n = _flag_int("--min-n") or 25
        rows = load_product_space(apps=_flag_list("--apps"))
        dens = compute_density(rows, min_n=min_n, db=store)
        if "--csv" in sys.argv:
            import csv, os
            out = "exports/product_density.csv"
            os.makedirs("exports", exist_ok=True)
            with open(out, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["product_group_id", "category", "local_density",
                            "knn_distance", "neighbor_count", "neighbor_brands",
                            "coverage_n", "coverage_age_days",
                            "insufficient_coverage", "stale_coverage"])
                for d in dens:
                    w.writerow([d["product_group_id"], d["category"], d["local_density"],
                                d["knn_distance"], d["neighbor_count"], d["neighbor_brands"],
                                d["coverage_n"], d["coverage_age_days"],
                                d["insufficient_coverage"], d["stale_coverage"]])
            print(f"[product-density] wrote {len(dens)} rows -> {out}")
        else:
            guarded = sum(1 for d in dens if d["insufficient_coverage"])
            stale = sum(1 for d in dens if d["stale_coverage"])
            cands = sparse_candidates(dens)
            print(f"[product-density] {len(dens)} groups · "
                  f"{guarded} in insufficient-coverage categories (excluded) · "
                  f"{stale} stale · {len(cands)} trustworthy sparse candidates")
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

    if "--embed-catalog" in sys.argv:
        # One-time (resumable) backfill: vectorize every distinct product
        # name in catalog_snapshots + watchlist + price_obs via the
        # Gemini-compatible /embeddings endpoint (ai: config). Names already
        # in the embeddings table are skipped, so re-running after a failure
        # or after new catalog sweeps only embeds what's new.
        from src.embed import backfill_catalog
        backfill_catalog(cfg, cfg.get("db", "deals.db"),
                         limit=_flag_int("--limit"))
        return

    if "--similar" in sys.argv:
        # Semantic archive query: names most similar to the phrase across
        # everything ever embedded. Needs the embeddings table (run
        # --embed-catalog first for whole-catalog coverage; search/watch
        # matching builds it lazily for names it actually sees).
        i = sys.argv.index("--similar")
        rest = sys.argv[i + 1:]
        q = rest[:rest.index("--limit")] if "--limit" in rest else rest
        query = " ".join(q)
        if not query:
            print("usage: python3 run.py --similar <product phrase> [--limit N]")
            return
        from src.embed import get_embedder
        emb = get_embedder(cfg, cfg.get("db", "deals.db"))
        rows = emb.most_similar(query, limit=_flag_int("--limit") or 10)
        if not rows:
            print(f"[similar] no vectors above threshold — run "
                  "`python3 run.py --embed-catalog` first")
            return
        for nm, s in rows:
            print(f"  {s:.2f}  {nm}")

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
        want_ui = "--ui" in sys.argv
        want_bot = "--bot" in sys.argv
        # --ui starts NOTHING by itself (dashboard only). Loops are opt-in:
        #   --ui --monitor          dashboard + glitch-monitor loop
        #   --ui --bot              dashboard + telegram bot (no monitor)
        #   --ui --bot --monitor    dashboard + bot + monitor
        # Bare --bot keeps its historic default (bot + monitor) unless
        # --no-monitor is passed. --no-monitor alongside --ui is an accepted
        # no-op (dashboard is already loop-free).
        if want_ui:
            want_monitor = "--monitor" in sys.argv
        else:
            want_monitor = "--no-monitor" not in sys.argv

        bot = None
        if want_bot:
            from src.tgbot import TGBot
            bot = TGBot(cfg)

        import threading
        if want_monitor:
            t = threading.Thread(target=loop, args=(cfg,), daemon=True)
            t.start()
        if want_bot and want_ui:
            # Dashboard keeps the foreground; bot polls in the background.
            tb = threading.Thread(target=bot.run, daemon=True)
            tb.start()

        parts = []
        if want_ui:
            parts.append("dashboard")
        if want_bot:
            parts.append("bot")
        if want_monitor:
            parts.append("monitor")
        print(f"[mode] {' + '.join(parts)}"
              + ("" if (want_bot or want_monitor)
                 else " only (no loops — add --bot/--monitor to opt in)"))

        if want_ui:
            import webbrowser
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
