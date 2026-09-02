# Catalog SKU counting — why "cumulative 5,919" ≠ "400 distinct" (and the fix)

**TL;DR** — The live `[sweep] … cumulative N SKUs` line is the **real** deduped
catalog size at that store. A verified uncapped Zepto sweep reached **5,919**
distinct SKUs, and the `products_full.json` dump length matched the final
`cumulative` line **exactly**. The crawler's stdout was **hard-capped at 400
products** (`[...products.values()].slice(0, 400)`), so only the first 400 ever
reached `catalog_snapshots` — **5,519 SKUs were silently discarded**. The "400
distinct" in the DB was the **cap**, not the catalog. Fixed 09-02: the cap is now
`--max-out` (default **20,000** — never truncates a real store). The next
`--catalog` sweep writes the full ~5,919.

*(The user's earlier run logged `cumulative 5,955`; the verified run logged
`5,919`. Both are the same phenomenon — the exact number varies slightly with
category-visit order and `--deep-cats` discovery. Neither is "400".)*

---

## 1. How the counting actually works

`tools/pw_catalog.js` keeps one in-memory `products` **Map** for the whole sweep:

- **Key** = `sku_key`: the product's `pvid:<uuid>` parsed from its URL
  (`/pn/<slug>/pvid/<uuid>`), falling back to a whitespace-stripped lowercase
  **name** when no pvid is present.
- Each category page visit parses its server-rendered **JSON-LD**
  (`collectLdJson`) and `set()`s every product into the Map. A repeat visit to a
  product already in the Map **updates** it (price/stock/url) and appends the new
  category to its `collections` list — it does **not** add a row.
- `products.size` therefore equals the number of **distinct SKUs seen so far**.
  That is exactly what the `[sweep] … cumulative N SKUs` line prints
  (`pw_catalog.js:1045`). It is a *deduped* running total, not a sum of raw hits.

At the end of the run:

```js
const list = [...products.values()].slice(0, MAX_OUT);   // was: slice(0, 400)
console.log(JSON.stringify({ …, products: list }));       // last stdout line
```

`run.py`/`watchlist.py` read **only that last stdout line**, so whatever the cap
truncates is **gone** — it never reaches the DB. With the old `400`, a store with
5,919 distinct SKUs silently contributed only 400.

> **So the "fishy" 5,919 → 400 is not double-counting and not a dedup bug.**
> 5,919 is the honest catalog size; 400 was an output ceiling. They were never
> measuring the same thing.

---

## 2. Two kinds of "repetition" — both normal, neither causes the gap

All figures below are from the **full uncapped** Zepto sweep (5,919 SKUs).

### (a) Cross-category membership (a SKU shelved in many categories)
The same product legitimately appears under several category pages (e.g. *Dragon
Fruit* is in *Fruits & Vegetables*, *Fresh fruits*, *All*, *New Launches*, …).
This is **correctly deduped** into one `sku_key`; only its `collections` list
grows. It inflates the *tag* count, **not** `products.size`.

| metric (full catalog) | value |
|---|---|
| distinct SKUs | 5,919 |
| total (SKU × category) tags | 7,558 |
| **avg categories per SKU** | **1.28** |

Category-membership distribution:

| appears in … | # SKUs | share |
|---|---|---|
| 1 category | 4,729 | 80% |
| 2 categories | 818 | 14% |
| 3 categories | 312 | 5% |
| 4 categories | 48 | |
| 5 categories | 8 | |
| 6 categories | 3 | |
| 7 categories | 1 | |

**Most SKUs live in exactly one shelf** — repetition is *low*, not high.

> ⚠️ **Cap-bias lesson:** the earlier capped 400-row snapshot reported avg
> **2.8** cats/SKU. That was an artifact — the cap kept the *first-inserted*
> SKUs, which are disproportionately the ones that recur across the early
> categories. The true full-catalog figure is **1.28**. Never trust a statistic
> computed on a capped slice.

### (b) Variant multiplicity (same name, different pack sizes)
"Kellogg's Corn Flakes Original" sold as 300 g / 500 g / 1 kg … each has its own
`pvid`, so each is a **distinct SKU** — correct, not a duplicate. Full catalog:
**5,919 keys / 5,364 names = 1.10×** variant multiplicity (~10% extra). So the
5,919 is ~5,364 distinct *products* + their variants — a real, large catalog.

---

## 3. Where repetition is most prominent (full 5,919-SKU catalog)

**Most multi-category SKUs:**

| cats | SKU | sample categories |
|---|---|---|
| 7 | Dragon Fruit Red | Fruits & Vegetables, Fresh fruits, All, New Launches |
| 6 | Custard Apple Semi Ripe | Fruits & Vegetables, Fresh fruits, All, New Launches |
| 6 | Pear Green Indian (Nashpati) | Fruits & Vegetables, Fresh fruits, All, New Launches |
| 6 | Snacky Apple | Fruits & Vegetables, Fresh fruits, All, Exotics & Premium |
| 5 | Monster Energy Ultra Zero Sugar | Cold Drinks & Juices, Top Picks, Diet & Lites, Energy Drinks |
| 5 | Milky Mist Skyr | Curd, Curd & Probiotic Drink, High Protein, Gut friendly |
| 5 | Godrej Real Good Chicken Breast Boneless | Frozen Food & Ice Creams, Frozen Meat, Top Picks, Raw Meats |
| 5 | Prasuma / Yummiez chicken sausages | Frozen Food & Ice Creams, Cold Cuts, Top Picks, Sausages |
| 5 | Meatzza Fresh Mutton Curry Cut | Frozen Food & Ice Creams, Frozen Meat, Top Picks, Raw Meats |

**Categories contributing the most SKUs:**

| SKUs | category |
|---|---|
| 187 | Top Picks |
| 66 | Zepto Cafe |
| 64 | Gifting |
| 60 | Premium |
| 58 | Breakfast & Sauces |
| 56 | Sweet Cravings |
| 47 | Milk Drinks |
| 44 | Dessert Mixes |
| 42 | Cold Coffee & Iced Tea / Fresh Bakery |
| 38 | Dates & Seeds |
| 37 | Combos |

**Products with the most variant SKUs (432 products have >1 variant):**

| variants | product |
|---|---|
| 11 | Rakhi for Brother |
| 7 | Daily Good Cashew · Coloressence Cute Coats Nail Paint |
| 5 | Kellogg's Corn Flakes Original · Red Bull Energy Drink · Tide Plus Jasmine & Rose Detergent · Lizol Citrus Floor Cleaner · Plntex Liquid Soap Dispenser |
| 4 | Harpic Original Toilet Cleaner · MAGGI 2-Minute Instant Noodles |

---

## 4. Why this matters for Demand Radar

`catalog_snapshots` is the **per-store baseline** for `new` / `delisted` churn
(`catalog_events`). With the 400 cap, every baseline was a truncated slice, so:

- the "catalog universe" looked ~15× smaller than reality, and
- churn diffs were computed against an **incomplete** catalog — a SKU that was
  simply never in the first 400 could be mis-flagged as `new` when it finally
  surfaced.

After the fix, the baseline is the store's full catalog.

---

## 5. Caveat — the first full sweep after lifting the cap

The previous Zepto baseline was **400** (capped). The first full sweep (~5,919)
diffed against that 400 will report **~5,500 "new" SKUs** — an **artifact of
lifting the cap**, not real new listings. To keep churn honest, **re-baseline**:
delete the old capped snapshot for the store (or treat the first post-fix sweep
as the new baseline) so `new`/`delisted` tracking starts from a complete catalog.

---

## 6. The fix (09-02)

`tools/pw_catalog.js`:
- `const MAX_OUT = parseInt(arg('max-out', '20000'), 10);` and
  `slice(0, MAX_OUT)` — the 400 ceiling is gone; `--max-out` overrides.
- When `DSH_DEBUG_DIR` is set, the run now writes **`products_full.json`** (the
  complete uncapped product set) so the live `cumulative` counter can always be
  reconciled against what is emitted.

`AGENTS.md` Commands: note added under `--build-watchlist` explaining that
`cumulative N` is the real deduped count and the old 400 cap is lifted.

**Verification (done):** a direct uncapped sweep
(`DSH_DEBUG_DIR=/tmp/zfull node tools/pw_catalog.js --app zepto … --categories
300 --deep-cats 1`) finished with final `[sweep] All -> cumulative 5919 SKUs` and
`products_full.json` containing **5,919** entries — an exact match, confirming
`products.size` is the real catalog and the old cap truncated 5,919 → 400.
