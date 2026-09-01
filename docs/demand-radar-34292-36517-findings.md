# Demand Radar: stores 34292 & 36517 — why high obs ↔ vanished SKUs ↔ zero open OOS

Investigation (09-01 → 09-02). Goal: explain why these two stores show very
high observation counts yet a pile of **vanished** SKUs and (until recently)
**zero open OOS** events.

## TL;DR

- Both stores are the **oldest, largest watchlists** in the corpus, so their
  raw observation counts are high by construction — not because of unusual
  demand.
- The vanished events are a **watchlist-rebuild artifact**, not real
  disappearances: the rebuild rotated which search-terms are covered, SKUs
  outside the top-40 `probe_terms_max` budget fell out of coverage, hit
  `vanished_cycles` (6) absent sweeps, and opened `kind='vanished'` events.
- "Zero open OOS" was a **snapshot artifact of the debounce + bursty cadence**,
  not an absence of stock-outs. The debounce (`oos_debounce_snapshots: 6`)
  needs 6 consecutive zero-reads; sweeps were clustered into hours 11–14 and
  17–22 with gaps up to 83–123 h, so the mass zero-reads only *completed* the
  debounce on the 09-01 11:28 sweep — which is when 50 `oos` events (ids
  209–258) opened, backdating `started_at` to the first zero read
  (08-30 22:29:58).
- A **live prober was running** during the investigation, so observations
  appeared in `deals.db` between queries — the "zero open OOS" snapshot was
  stale almost immediately.

## Store scale (why the counts are high)

| store | observations | SKUs seen | active watchlist |
|------:|-------------:|----------:|-----------------:|
| 34292 | 8 883 | 1 773 | 847 |
| 36517 | 7 812 | 1 469 | 663 |

These are the two biggest watchlists by far, so they accumulate the most
`sock_obs` rows. High obs ≠ high demand; it is mostly history depth.

## Vanished events — a rebuild artifact

- 34292: **42 vanished** events (39 still open). 36517: **44 vanished**
  (31 open). They are the **only** two stores with any vanished events.
- Both batches opened in a tight burst on **08-25 13:27:11** (34292, 42) and
  **08-25 13:56:05** (36517) — i.e. immediately after a `--build-watchlist`
  rebuild.
- Cause: the rebuild re-ranches `watchlist_terms` (distinct `q:` labels among
  active rows, most-covering first, capped at `probe_terms_max = 40`). SKUs
  whose search term fell outside the top-40 budget were simply never re-probed
  each sweep, so their absence accumulated to `vanished_cycles = 6` and they
  opened `kind='vanished'`.
- **Zombie reconciliation gap:** 38 of 34292's 42 vanished SKUs are now
  `active = 0` (deactivated by the rebuild). The event machine only visits
  `active_skus | open_oos` — it does **not** re-visit an open `vanished` event
  sitting on an inactive SKU — so those 39 open vanished events never
  reconcile and stay open forever. (18401 @ 34292: in-stock 4× on 08-24 only,
  never seen since → vanished opened 08-25 13:27:11.)

## "Zero open OOS" — debounce + bursty cadence

- `oos_debounce_snapshots: 6` means an `oos` event needs **6 consecutive**
  zero-reads; any 1 in-stock read closes it; nulls pause (don't reset).
- The prober ran in **bursty clusters**: sweeps landed in hours 11–14 and 17–22
  with long gaps (83.7–123.1 h between some sweeps). The mass zero-reads that
  matter (apparel/innerwear/dairy terms — e.g. `briefs` qty 34, `t-shirt`
  qty 17) needed 6 reads spanning **08-30 22:29 → 09-01 11:28**. The 09-01
  11:28 sweep completed the debounce, opening 50 `oos` events (ids 209–258)
  with `started_at` backdated to 08-30 22:29:58 (restart-proof streak).
- Before that sweep closed, a query would have reported **zero open OOS** even
  though the underlying stock-outs were real and days old.

### 36517's 3 open OOS (ids 63/64/150)

- 690024 / 690025 = "Organic Acre Nutrition Desi Kadaknath Eggs" (`q: eggs`),
  **never in-stock** across all 8 sightings 08-25 → 08-31.
- id 63 `started_at` 13:13:33; the 11:19:58 sweep that day was almost certainly
  a **suspect/frozen cycle** (canary-only-OOS freeze), so the event only
  opened on the first *post-thaw* zero read.

## Hour-coverage gaps (the silent hours)

Observations exist in hours: **01, 11, 12, 13, 14, 17, 18, 19, 20, 21, 22**.
**Missing entirely: 00, 02–10, 15, 16, 23.**

Consequence: any OOS that onsets during 02–10 or 15–16 is only *recorded* at
the next sweep after the gap — its `started_at` is the first read after the
gap, so the **onset heatmap is biased toward sweep hours**. The DPI rollups
ignore `kind='vanished'` (only `kind='oos'` counts), so the vanished zombies
do not corrupt DPI, but they inflate the raw event counts.

## Fix shipped alongside this report

`demand.sweep_windows` (local hours; `"0-23"` = all) now gates when the
`--demand` loop **starts** rounds. Default stays 24/7. Operators can set e.g.
`["2-10","15-16"]` to deliberately fill the silent windows; the loop idles
outside the window and never stalls a round already in flight. See the
"Onset windows need silent-hour coverage" invariant in `AGENTS.md` and the
README run-command note.

## Recommendations (data hygiene, not yet coded)

1. **Reconcile stale vanished zonbies:** when a `vanished` event's SKU is
   `active=0`, either close the event or keep it open but exclude from
   "open vanished" tallies. This removes the 39+31 phantom open events.
2. **Run the prober 24/7** (or set `sweep_windows` to the silent windows) so
   onsets in 02–10 / 15–16 are captured at onset, not backdated to the next
   sweep.
3. **Avoid watchlist rebuilds mid-probe** for the two largest stores, or
   re-seed coverage so the top-40 term budget doesn't drop whole SKU classes
   (which is what fabricated the vanished events).
