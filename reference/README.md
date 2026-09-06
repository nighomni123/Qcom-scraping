# Reference product databases

Read-only side tables used to **align / validate** the product records parsed by
`src/product_fields.parse_name` (M1) and to bootstrap the M2 vector work.

These are reference data only — they are NOT joined into `deals.db` (which must
stay schema-untouched per AGENTS.md invariants). Pull once, keep them here.

## What's here

| File | Source | Rows | Has pack-size? | License | Notes |
|---|---|---|---|---|---|
| `amazon_india_products.csv` | PromptCloud Amazon-India sample (Oct 2019) via HF `pgurazada1/amazon_india_products` | 30,000 | ✅ `Pack Size Or Quantity` (99%) | research use | personal-care–heavy; 8,455 brands |
| `BigBasket.csv` | Kaggle `chinmayshanbhag/big-basket-products` (user-fetched) | 8,208 | ✅ `Quantity` (100%) | Kaggle dataset | India grocery; cleanest pack source ("2 kg", "12 pcs") |
| `BigBasket Products.csv` | Kaggle `surajjha101/bigbasket-entire-product-list-28k-datapoints` (user-fetched) | 38,341 | ❌ | Kaggle dataset | `index, product, category, sub_category, brand, sale_price, …` — huge brand/category vocab, no pack column |
| `Indian Packaged Foods Nutritional Composition Data/packaged_foods_india.csv` | Mendeley Data (user-fetched) | 852 | ⚠️ `Serving_Size_g` (not pack) | Elsevier/Mendeley | nutrition + `Brand_Name`, `Item name`, `Category`, `Price_INR` — good held-out eval |
| `openfoodfacts_india.csv` | Open Food Facts, `countries_tags=en:india` | 999 (partial) | ✅ `quantity` | ODbL (open) | food-focused; **resumable fetch** — see below |

## Best pack-size ground truth
Two files carry a dedicated pack-size column — use them to measure `parse_name`
accuracy and to recover pack/unit/variant on parser misses:
- **`BigBasket.csv` → `Quantity`** (100% filled, India grocery) — primary.
- **`amazon_india_products.csv` → `Pack Size Or Quantity`** (99% filled) — secondary,
  broader category mix (incl. personal care).

## How the files were obtained
- `amazon_india_products.csv`: direct HF download, no auth.
- `BigBasket.csv`, `BigBasket Products.csv`, and the Mendeley folder: fetched by
  the user (Kaggle login / Mendeley download).
  - `BigBasket.csv` ← https://www.kaggle.com/datasets/chinmayshanbhag/big-basket-products
  - `BigBasket Products.csv` ← Kaggle `surajjha101/bigbasket-entire-product-list-28k-datapoints`
- `openfoodfacts_india.csv`: via `fetch_off_india.py` (public API, no auth). OFF
  edge-blocked this IP during the pull (HTTP 503/401), so only 999 rows landed.
  The script is **resumable** — re-run it later and it appends from the last page:
  ```bash
  python3 reference/fetch_off_india.py
  ```
  Progress is tracked in `.off_india_progress`; the CSV grows each run as OFF's
  rate limit lifts.

## Deliberately skipped (not in this folder)
- **GS1 DataKart** — login-gated B2B GTIN registry, no public bulk access.
- **ONDC** — a network protocol, not a downloadable catalog.
- **Commercial "Blinkit/Zepto dataset" vendors** (fooddatascrape, actowiz,
  brightdata, webdatainsights) — they re-scrape what we already crawl ourselves.
- **Kaggle "Quick Commerce Dataset" / "Blinkit Products Dataset"** — synthetic
  practice datasets (Order_ID/City or fat/protein columns), not real catalogs.

## Future idea (user): publish our own scraped catalog to Mendeley Data
Mendeley Data is an open research-data repository (Elsevier) — a natural home for
the qcom-scraping product catalog once it's worth sharing. Considerations for
later: the captured data is product-level (names/prices/pack/category), no
personal data, so redistribution risk is low; choose a license (CC0 / CC-BY) at
upload; respect the source apps' ToS on republishing aggregated catalog data.
Not actioned now.
