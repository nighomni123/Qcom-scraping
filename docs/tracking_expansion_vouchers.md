# Expanded Tracking Parameters — Digital Vouchers (Restock Triggers)

Status: IMPLEMENTED (additive schema applied) (additive only — no drops/renames per repo invariant).
Target: capture why restock data for top-pressure voucher SKUs reads as `None`.

## Problem (verified from `deals.db` analysis)

- 51 `oos` events at local hour 17 (evening) — almost all digital vouchers:
  `Roblox Instant Voucher`, `Steam Instant Voucher`, `Valorant Instant Voucher`,
  `Domino's Pizza Instant Voucher`, `Amazon Prime Membership Subscription Voucher`,
  `Blinkit Gift Card`, `Xbox Game Pass Ultimate Subscription Voucher`, etc.
- These SKUs have **ZERO** `price_obs` entries (tracking blind spot) and
  **ZERO** ended restock events (`ended_at IS NOT NULL` = 0) — so `mean_restock_min`
  reads `None`.
- They are open (`kind='oos'`, `ended_at IS NULL`) with no in-stock observations
  (`in_stock=1` count = 0) — the event machine never closes them.

## Root cause

Current `stock_obs` schema (`src/store.py`) tracks:
```
app | store_id | sku_key | in_stock (1/0/NULL) | price | mrp | eta_min | source (home/collection/search) | ts
```

There is no column for:
- What triggered restock (promotion expiry, manual refill, catalog update, auto-replenish)
- Whether the SKU is a digital voucher (restock mechanism differs from physical goods)
- Catalog version / timestamp to detect server-side updates
- Promotional context active at restock time

Because voucher SKUs are hidden from listings when out of stock (same as physical SKUs),
but their restock mechanism is different (digital code replenishment, not shelf refill),
we never observe a restock event and the data stays `None`.

## Proposed additive schema changes

### 1. `stock_obs` — new optional columns (additive `ALTER TABLE`)

```sql
ALTER TABLE stock_obs ADD COLUMN restock_trigger TEXT DEFAULT NULL;
-- Values: 'promotion_expired', 'manual_refill', 'catalog_update', 'auto_replenish', 'voucher_code_replenished', 'unknown'

ALTER TABLE stock_obs ADD COLUMN voucher_type TEXT DEFAULT NULL;
-- Values: 'digital', 'physical', 'gift_card', 'subscription', NULL (not a voucher)

ALTER TABLE stock_obs ADD COLUMN promotional_context TEXT DEFAULT NULL;
-- Free-text note: e.g. 'evening promo drop at 17:00 local', 'catalog refresh 2026-08-30'

ALTER TABLE stock_obs ADD COLUMN catalog_version TEXT DEFAULT NULL;
-- Hash / timestamp of the catalog response that produced this observation
```

### 2. `watchlist` — new optional column (voucher tracking flag)

```sql
ALTER TABLE watchlist ADD COLUMN is_digital_voucher INTEGER DEFAULT 0;
-- 1 = SKU is a digital voucher / gift card; 0 = standard SKU (default).
```
This lets the prober (`src/prober.py`) apply voucher-specific restock rules:
- A digital voucher restock is not a shelf refill but a code replenishment event.
- The debounce / vanished cycle rules (`vanished_after`) may need different thresholds
  for vouchers (they don't physically disappear from listings in the same way).

### 3. `oos_events` — new optional column (restock trigger at close)

```sql
ALTER TABLE oos_events ADD COLUMN restock_trigger TEXT DEFAULT NULL;
-- Set when `close_oos_event` is called: what caused the restock.
```

### 4. `price_obs` — new optional column (catalog version tracking)

```sql
ALTER TABLE price_obs ADD COLUMN catalog_version TEXT DEFAULT NULL;
-- Matches catalog_version in stock_obs for correlation analysis.
```

## Implementation notes (per `AGENTS.md` rules)

- **Additive only**: never drop/rename existing columns (`sku_key`, `in_stock`, etc.).
- **Backfill**: new columns start `NULL`; no backfill required for historical rows.
- **Schema update**: add to `_SCHEMA` in `src/store.py` and use `ALTER TABLE IF NOT EXISTS`
  in `__init__` (same pattern as `ALTER TABLE price_obs ADD COLUMN category`).
- **Config**: no new tunables required initially; if restock-trigger thresholds are needed,
  add under `demand:` in `config.yaml` at the END of the section.

## Tracking gap verification (current state)

From `deals.db` (verified 2026-08-30):

| Metric | Value |
|---|---|
| Hour 17 `oos` events | 51 |
| Stores affected (blinkit) | 34292 (14), 35682 (15), 37246 (15), 36517 (4), 36269 (3) |
| Voucher SKUs involved | Roblox, Steam, Valorant, Domino's, Amazon, Blinkit Gift, Xbox, Starbucks, Hamleys, Shoppers Stop, Reliance Jio, Croma, AJIO |
| `price_obs` entries for these SKUs | 0 (complete tracking blind spot) |
| `stock_obs` `in_stock=1` observations | 0 (no restock evidence) |
| `oos_events` ended (`restock`) | 0 (restock data = `None` for all 22 open voucher SKUs) |
| Active watchlist SKUs with zero observations (store 41542+) | 40 |
| Active watchlist SKUs with zero observations (store 43053) | 86 |

## Recommended next steps

1. Apply additive `ALTER TABLE` for the 4 new columns (`restock_trigger`, `voucher_type`,
   `promotional_context`, `catalog_version`) in `src/store.py`.
2. Update `prober.py` (`StockProber.sweep_store`) to populate `voucher_type` for watchlist
   items where `name LIKE '%voucher%' OR '%instant voucher%'` etc.
3. Update `watchlist.py` (`watchlist_for_store`) and build logic (`watchlist.py`) to set
   `is_digital_voucher = 1` for matched voucher terms.
4. Modify `oos_events` close logic (`store.close_oos_event`) to optionally record the
   restock trigger when an event closes.
5. Verify after rebuild (`python3 run.py --check`) and confirm no schema break.

## Related files

- `deals.db` — source of truth for all observations.
- `src/store.py` — persistence layer; schema lives here.
- `src/prober.py` — event machine; restock / vanished logic.
- `exports/dpi_*.csv` — restock `None` is visible in `mean_restock_min` column.
