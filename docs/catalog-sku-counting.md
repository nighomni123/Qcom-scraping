# Catalog SKU counting — why "cumulative 5,955" ≠ "400 distinct" (and the fix)

**TL;DR** — The live `[sweep] … cumulative N SKUs` line is the **real** deduped
catalog size at that store (Zepto ≈ **5,955**). The crawler's stdout was
**hard-capped at 400 products** (`[...products.values()].slice(0, 400)`), so only
the first 400 ever reached `catalog_snapshots`. The "400 distinct" in the DB was
the **cap**, not the catalog. Fixed 09-02: the cap is now `--max-out`
(default **20,000** — never truncates a real store). The next `--catalog` sweep
writes the full ~5,955.

---

## 1. How the counting actually works

`tools/pw_catalog.js` keeps one in-memory `products` **Map** for the whole sweep:

- **Key** = `sku_key`: the product's `pvid:<uuid>` parsed from its URL
  (`/pn/<slug>/pvid/<uuid>`), falling back to a whitespace-stripped lowercase
  **name** when no pvid is present.
- Each category page visit parses its server-rendered **JSON-LD**
  (`collectLdJson`) and `set()`s every product into the Map. A repeat visit to a
  product that's already in the Map **updates** it (price/stock/url) and appends
  the new category to its `collections` list — it does **not** add a row.
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
5,955 distinct SKUs silently contributed only 400.

> **So the "fishy" 5,955 → 400 is not double-counting and not a dedup bug.**
> 5,955 is the honest catalog size; 400 was an output ceiling. They were never
> measuring the same thing.

---

## 2. Two kinds of "repetition" — both normal, neither causes the gap

### (a) Cross-category membership (a SKU shelved in many categories)
The same product legitimately appears under many category pages (e.g. *Dragon
Fruit* is in *Fruits & Vegetables*, *Fresh fruits*, *All*, *New Launches*, …).
This is **correctly deduped** into one `sku_key`; only its `collections` list
grows. It inflates the *tag* count, **not** `products.size`.

From the last Zepto snapshot (400 captured SKUs):

| metric | value |
|---|---|
| distinct SKUs captured | 400 |
| total (SKU × category) tags | 1,119 |
| **avg categories per SKU** | **2.8** |

Category-membership distribution:

| appears in … | # SKUs |
|---|---|
| 1 category | 37 |
| 2 categories | 136 |
| 3 categories | 130 |
| 4 categories | 78 |
| 5 categories | 15 |
| 8 categories | 3 |
| 9 categories | 1 |

### (b) Variant multiplicity (same name, different pack sizes)
"Kellogg's Corn Flakes Original" sold as 300 g / 500 g / 1 kg … each has its own
`pvid`, so each is a **distinct SKU** — correct, not a duplicate. In the snapshot
**400 keys / 371 names = 1.08×** variant multiplicity (only ~8% extra). So the
5,955 is ~5,500 distinct *products* + their variants — a real, large catalog.

---

## 3. Where repetition is most prominent

> ⚠️ The table below is from the **capped 400-SKU snapshot**, so it is biased to
> the **first-visited** categories (fresh produce, staples). The full-catalog
> ranking (all ~5,955 SKUs, every category) is refreshed here after the uncapped
> sweep completes.

**Most multi-category SKUs (capped snapshot):**

| cats | SKU | sample categories |
|---|---|---|
| 9 | Dragon Fruit Red | Fruits & Vegetables, Fresh fruits, All, New Launches… |
| 8 | Custard Apple Semi Ripe | Fruits & Vegetables, Fresh fruits, All, New Launches… |
| 8 | Pear Green Indian (Nashpati) | Fruits & Vegetables, Fresh fruits, All, New Launches… |
| 8 | Snacky Apple | Fruits & Vegetables, Fresh fruits, All, Exotics & Premium… |
| 5 | Organically Grown Chilli / Garlic / Spinach | Fruits & Vegetables, All, Organics & Hydroponics, Leafy… |
| 5 | Toyo Kombucha Low Sugar Pineapple | Tea/Coffee, Cold Coffee & Iced Tea, Non-Alcoholic Drinks… |
| 5 | Anveshan / Borges cold-pressed oils | Atta, Rice, Oil & Dals, Refined oil… |

**Categories contributing the most SKUs (capped snapshot):**

| SKUs | category |
|---|---|
| 43 | Top Picks |
| 39 | Tea |
| 32 | Fruits & Vegetables / All / Frozen Food / Veg Snacks / Ice Creams / Tubs / Packaged Food / Masala / … |

**Products with the most variant SKUs (capped snapshot):**

| variants | product |
|---|---|
| 5 | Kellogg's Corn Flakes Original |
| 3 | Brooke Bond Red Label Tea |
| 2 | GTS Original Kolam Rice · McCain French Fries · Banana Robusta · Borges Olive Oil · Daily Good Cashew/Figs/Pumpkin Seeds · Godrej Yummiez Cheese Corn Nuggets |

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

The previous Zepto baseline was **400** (capped). The first full sweep (~5,955)
diffed against that 400 will report **~5,555 "new" SKUs** — an **artifact of
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

**Verification:** `node --check tools/pw_catalog.js`; a direct uncapped sweep
(`DSH_DEBUG_DIR=… node tools/pw_catalog.js --app zepto … --categories 300
--deep-cats 1`) dumps `products_full.json` whose length matches the final
`cumulative` line — confirming the Map size (real catalog) and the emitted list
(now equal, post-fix).
