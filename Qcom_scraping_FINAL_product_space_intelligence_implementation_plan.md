# Product-Space Intelligence Engine — Tailored to `Qcom-scraping`

## 0. What's different from the generic plan, and why

The original plan is written for a generic "we have a product catalog with rich
specs, ratings, and reviews" setting (its running example is ANC headphones).
`Qcom-scraping` is a single-operator Mumbai quick-commerce tracker (Blinkit,
Zepto, Instamart) with none of those assumptions. This version keeps the
original's good ideas — layered vectors, the gap-severity ladder, LLM-as-
interpreter — but rebuilds every phase around five hard constraints:

1. **No ratings/reviews/order-volume data exists on any tracked app.** The repo
   already discovered this and built the Demand Pressure Index (DPI:
   Σ OOS-minutes × recency ÷ observation-days) as the only honest demand
   proxy. Any "commercial signal" phase must use DPI + catalog churn, not
   invented review-count fields.
2. **The catalog is FMCG/grocery, not durable goods.** Battery life, screen
   size, connectivity, water resistance — none of this applies to milk,
   snacks, and personal care. The attribute schema has to be pack
   size/quantity, unit price, variant/flavor, brand, and category depth.
3. **Data lives in four separate SQLite files**, not one table:
   `deals.db` (price_obs, searches, watchlist, stock_obs, oos_events,
   catalog_snapshots, catalog_events) plus per-app
   `inventory/inventory_blinkit.db` / `inventory_zepto.db` /
   `inventory_instamart.db`. Any cross-app analysis has to start with a
   union/normalization layer — this is real work, not a formality.
4. **Crawl cadence is slow and serialized.** A full catalog snapshot is
   20–60 minutes per store and must run alone (not alongside `--demand` /
   `--map-locality` / `--build-watchlist`, or sessions get rate-limited into
   empty probes). Realistically that's ~1 store/day. Gap-detection thresholds
   have to be sized for hundreds–low-thousands of SKUs per store, not tens
   of thousands, and for infrequent refresh.
5. **The repo already has real, working infrastructure to extend, not
   replace**: `--embed-catalog` (resumable, NVIDIA nemotron-embed-vl-1b-v2 @
   2048d or local Ollama embeddinggemma-300m @ 768d), `embed_3d_map.py`
   (PCA→3D atlas with Semantic/Price/Store/Category/Density modes, cluster
   explorer, product inspector, optional `--projection umap`), and a
   dashboard AI-assistant panel that already writes markdown reports with
   downloadable links and appendable follow-up Q&A. New work should look like
   an extension of these, not a parallel system.

Given that, the guiding principle from the original plan still holds, with one
addition:

> Find sparse but plausible regions inside meaningful product neighborhoods,
> then validate against attributes, DPI, catalog churn, and cross-app
> assortment — **and never forget that "sparse" in this dataset is as likely
> to mean "we haven't crawled it yet" as "no one sells it."**

---

## Phase 1 — Grocery-Shaped Product Records (not the generic attribute list)

**Goal:** Replace the plan's electronics-flavored attribute schema with one
that matches what's actually in `price_obs` / `watchlist` / `catalog_snapshots`.

### Fields to add (extending, not replacing, existing tables)

- **Identity:** product name (already have), brand (extract from name —
  most SKUs are `Brand + variant + pack size`, e.g. "Amul Taaza Toned Milk
  500ml"), category/subcategory (already produced by `src/categories.py`'s
  keyword classifier), pack size + unit (parse from name: `500ml`, `1kg`,
  `12-pack`).
- **Structured attributes that actually exist in this domain:**
  `pack_size_value`, `pack_size_unit`, `unit_price` (price ÷ pack size —
  this is the single most useful derived numeric field for a grocery
  catalog and the plan's original schema has no equivalent of it),
  `variant`/`flavor` (parsed from name where possible), `is_multipack`.
- **Commercial attributes already collected, just not joined:** current
  price, MRP, discount %, stock status, store_id/app, historical presence
  (`catalog_snapshots`), DPI (once computed), `first_seen_ts`,
  `active` flag from `watchlist`.
- **Explicitly drop from the generic schema:** battery life, screen size,
  connectivity, water resistance, storage, power, materials, compatibility —
  these don't occur in this catalog and including them just produces
  all-null columns.

### Tasks

1. Write a lightweight name parser (`src/product_fields.py`) that extracts
   brand / pack size / unit / variant from the free-text product name using
   regex + a small unit-conversion table (ml/l, g/kg, pack counts). Log
   parse failures rather than silently dropping — with a corridor of Indian
   grocery names, expect ~70–85% clean parses on the first pass; that's fine,
   just track the miss rate.
2. Add `unit_price` as a stored, derived column so later phases don't
   recompute it per query.
3. Do **not** touch the scraping layer (`tools/pw_catalog.js`,
   `src/adapters/*`) to try to pull specs pages — quick-commerce apps don't
   expose them, and Phase 8's Akamai/IP-lock findings show how expensive
   fighting these platforms for extra fields already is. This phase is a
   parsing/derivation layer over data you already have, not new scraping.

### Deliverable

A `products` view (SQL view, not a new physical table, to avoid a second
source of truth) per app-DB that joins `watchlist` + latest `stock_obs` +
`catalog_snapshots` + parsed fields, usable directly by the embedding phase.

**Success criterion:** for a sampled 200-SKU set across categories, brand and
pack-size parse accuracy ≥ 80%, and every SKU has a `unit_price` or an
explicit `unparseable` flag (never a silent null standing in for zero).

---

## Phase 2 — A Union Layer Across the Four Databases

**Goal:** Make "the product space" queryable as one thing, without merging
the databases (each app-DB's schema and lock discipline should stay as-is —
merging risks breaking the existing anti-block/session isolation invariants
in `AGENTS.md`).

### Tasks

1. Build `src/product_space.py` with a single function,
   `load_product_space(apps=None, since=None) -> DataFrame`, that opens each
   `inventory_<app>.db` (read-only) plus `deals.db`, applies the Phase 1
   parsing, and returns one normalized frame keyed by
   `(app, store_id, sku_key)`.
2. Attach a stable **cross-app product identity** where possible: same
   brand + normalized name + pack size across apps → same `product_group_id`.
   This is what makes Phase 6 (assortment gaps between apps) possible at
   all — without it, "Blinkit has X but Zepto doesn't" can't be computed.
   Use fuzzy matching at the same token-overlap threshold (≥0.5) the
   existing `search.py` fuzzy-matcher already uses, for consistency.
3. Exclude vouchers using the existing `src/store.py → is_voucher_name()`
   matcher — don't re-derive this.

### Deliverable

One in-memory (or cached-to-parquet) product-space table that every later
phase reads from, instead of each phase re-querying four SQLite files.

**Success criterion:** running `load_product_space()` against current data
completes without touching any live crawl session, and cross-app grouping
recovers a plausible match rate on a manually-checked 50-item sample
(e.g., "Amul Taaza Toned Milk 500ml" on Blinkit groups with "Amul Taaza Milk
500 ml" on Zepto).

---

## Phase 3 — Extend the Existing Embedding Pipeline (don't rebuild it)

**Goal:** Add attribute and commercial vectors alongside the semantic vector
`--embed-catalog` already produces, instead of building a parallel embedding
system.

### A. Semantic vector — reuse as-is

The existing resumable, cached `--embed-catalog` pipeline
(nemotron-embed-vl-1b-v2 or local embeddinggemma) already does this well.
One improvement: feed it the Phase 1 parsed fields as structured context
(`"{brand} | {category} | {variant} | {pack_size}"`) prepended to the raw
name, rather than raw name alone — this is a text-construction change inside
the existing embed step, not a new pipeline.

### B. Attribute vector — new, small, grocery-shaped

```
unit_price
pack_size_value (normalized to a common unit per category)
is_multipack
category_depth (how specific the categories.py classification got)
```

Deliberately short. A 4–6 dimension numeric vector, z-scored per category
(price and pack size only make sense compared within a category — ₹500 is
cheap for a headphone-adjacent electronics SKU and expensive for a spice
packet).

### C. Commercial vector — built from DPI and churn, not invented signals

```
dpi (from existing Demand Radar rollup)
store_count (how many darkstores carry this product_group_id)
app_count (how many of the 3 apps carry it)
days_since_first_seen
is_currently_active (watchlist.active)
recent_churn_flag (appeared/disappeared in last N catalog_events, excluding
                    the pair-count-collapse and rekey-reconciled events)
```

This is the phase where the original plan's "price, discount, availability,
review count..." list gets replaced wholesale with what this repo can
actually measure.

### Deliverable

Each `product_group_id` has `semantic_vector`, `attribute_vector`,
`commercial_features`, cached to disk keyed by the same resumable pattern
`--embed-catalog` uses, so re-runs skip already-computed groups.

**Success criterion:** re-running after a `--build-watchlist --catalog`
sweep only recomputes vectors for new/changed `product_group_id`s.

---

## Phase 4 — Extend `embed_3d_map.py`, Don't Replace It

**Goal:** The atlas already exists with Semantic / Price / Store / Category /
Density modes, a cluster explorer, and a product inspector. Add to it rather
than redesigning the layout from scratch.

### Tasks

1. Add a **Gap mode** alongside the existing mode selector: colors/shades
   points by local kNN distance (Phase 5) instead of by cluster.
2. Add **unit_price** as a coloring option next to the existing Price mode
   (raw price is misleading across pack sizes; unit price is the fair
   comparison).
3. Add a **cross-app overlay toggle**: since `product_group_id` links the
   same product across apps, let a point's marker shape indicate which
   apps carry it (matches the existing darkstore map's ▲/◆/■ per-app
   convention on the dashboard's Working Area map — reuse that visual
   language for consistency).
4. Keep `--sample N` and the existing `--projection umap` flag as-is; add
   density/gap computation as a post-projection step so it works with either
   projection.

### Deliverable

The same self-contained HTML export (`exports/embedding_map[_<model>].html`)
with two new modes, no new UI framework.

**Success criterion:** file still opens standalone (no server dependency,
matching the current design), and load time doesn't regress materially with
the current catalog size.

---

## Phase 5 — Density & Gap Detection, Sized for This Data Volume

**Goal:** Compute local density and kNN distance, but with explicit minimum-
sample guards so sparse categories don't produce fake "gaps."

### Tasks

1. Compute local density and kNN distance **per category**, not globally —
   a "sparse" point in a 40-SKU category means something different from one
   in a 2,000-SKU category.
2. **Minimum-N guard:** don't emit a gap candidate for any category with
   fewer than, say, 25 tracked SKUs across all three apps — below that, the
   apparent gap is far more likely to be "we haven't watchlisted this
   subcategory yet" than a real market gap. Surface an explicit
   `insufficient_coverage` flag instead of silence, so it's visible which
   categories need broader `--build-watchlist` coverage before gap analysis
   is trustworthy there.
3. Track **coverage age** per category: since full catalog snapshots run at
   ~1 store/day, a category last swept 3 weeks ago should be flagged
   `stale_coverage`, not treated as current.

### Deliverable

A density table: `product_group_id, category, local_density, knn_distance,
neighbor_count, neighbor_brands, coverage_n, coverage_age_days,
insufficient_coverage, stale_coverage`.

**Success criterion:** no gap candidate is ever produced from a category
flagged `insufficient_coverage` — this is the single most important guard
against reproducing the original plan's core failure mode (mistaking crawl
gaps for market gaps) given this repo's actual crawl cadence.

---

## Phase 6 — Gaps, Scoped to What's Actually Detectable Here

Given the constraints, only two of the original plan's four gap types are
realistically supportable in the near term:

### Assortment gap (highest-confidence, build this first)

"Product X exists on Blinkit/Zepto in this corridor but not on Instamart" —
directly computable from the Phase 2 union layer + `product_group_id`
matching, with no embedding math required. This is the gap type this repo's
existing data model is *best* suited for, since it already tracks the same
darkstores across three apps.

### Internal / attribute gap (second priority, with guardrails)

A sparse point surrounded by real neighbors in the same category, past the
Phase 5 minimum-N guard. Score using `attribute_vector` proximity (unit
price + pack size), since that's what's reliably available — not invented
feature combinations like the original plan's ANC/battery/weight example.

### Deferred: interpolation gap

Needs a well-populated attribute space (price × size × brand tier) per
category to interpolate meaningfully. Revisit once Phase 5's coverage flags
show most categories clearing the minimum-N guard — likely a Milestone 2+
concern, not now.

---

## Phase 7 — Commercial Scoring from DPI + Churn (replaces the original's
Phase 7 wholesale)

```
Opportunity Score
=
Assortment-gap strength (missing from N of 3 apps in this corridor)
× Neighborhood DPI (average demand pressure of nearby/competing SKUs)
× Coverage confidence (inverse of insufficient_coverage / stale_coverage)
× Churn stability (penalize categories with recent mass pair-count-collapse
                    events — that's crawl noise, not market signal)
```

Every component stays visible per the original plan's transparency
principle — but every component is something this repo can actually compute
today, with no placeholder fields.

**Success criterion:** every opportunity's score breakdown cites real
table/column provenance (e.g., "DPI computed from 14 days of stock_obs
across 3 stores"), so a human reviewer can immediately tell real signal from
thin data.

---

## Phase 8 — Validation, Wired to Guardrails This Repo Already Built

Don't re-derive data-quality checks the repo has already fought for — call
them directly:

- **Does it already exist?** Check `searches` / `search_results` (the full
  search archive) and the current `product_group_id` table before treating
  anything as a gap.
- **Is this a crawl artifact?** Reuse the existing pair-count-collapse guard
  (snapshot pair count < 70% of previous = flake, not churn) and the
  rekey-reconciliation logic (name+price pair matching across sweeps) —
  don't build a second version of either.
- **Is this sweep-hour biased?** If a candidate gap's evidence comes from
  `stock_obs` observations concentrated in non-silent hours, flag it — the
  repo's own README notes the 02–10 and 15–16 windows are undercounted
  unless `demand.sweep_windows` has been widened.
- **Is it a voucher?** Excluded upstream in Phase 2 already, but re-check at
  the validation boundary in case a future adapter reintroduces them.

**Deliverable:** every candidate carries a `validation_reason_codes` field
built entirely from existing repo signals — no new heuristics invented for
this step.

---

## Phase 9 — LLM Interpretation via the Existing AI Assistant Panel

**Goal:** Extend the dashboard's existing AI-assistant pattern instead of
building a new report system.

### Tasks

1. Add a fourth helper alongside the existing three ("Understand the
   results," "Tune probing methodology," "Choose product focus"): **"Explain
   this opportunity."** Same config gate (`ai.enabled`, `AI_API_KEY` or local
   Ollama base URL), same output shape — a markdown report at
   `exports/ai_explain_<stamp>.md` with a download link, and the same
   append-on-follow-up pattern already used for the other helpers.
2. Prompt content = candidate gap + nearest real products (names, prices,
   unit prices) + DPI + validation reason codes + coverage confidence.
   Explicitly instruct the model to distinguish measured evidence
   (DPI, coverage, price data) from speculation (why the gap might exist),
   matching the original plan's intent — the change here is just that the
   evidence fed in is real, repo-native data rather than placeholder fields.
3. Config writes (if the assistant proposes adjusting `demand.staple_queries`
   or similar) go through the same validated write path the location editor
   and "Choose product focus" helper already use — never a second config
   writer.

**Success criterion:** opportunity briefs read the same way the existing
Demand Radar explainer reports do, so this feels like one coherent AI
assistant rather than a bolt-on.

---

## Rollout Order (given single-operator, serialized-crawl constraints)

| Milestone | Contents | Why this order |
|---|---|---|
| **M1** | Phase 1 (parsing) + Phase 2 (union layer) | Pure data-layer work, no crawling, can be built and tested entirely offline against existing DBs |
| **M2** | Phase 3 (vectors) + Phase 4 (atlas extensions) | Builds on M1; reuses `--embed-catalog` cache, so cheap to iterate |
| **M3** | Phase 6 assortment gaps only | Needs no new embedding math — highest confidence, ships value fastest |
| **M4** | Phase 5 (density/gap w/ guards) + Phase 6 internal gaps | Gated on enough categories clearing the minimum-N coverage guard |
| **M5** | Phase 7 (scoring) + Phase 8 (validation) | Needs M3/M4 output to score against |
| **M6** | Phase 9 (LLM briefs) | Last, since it's presentation over everything upstream |

Note that **assortment gaps (M3) ship before density-based gaps (M4)** —
the reverse of the original plan's ordering — because assortment gaps need
no new statistics, just the union layer, and this repo's per-store crawl
cadence makes density-based gap detection the slower, more data-hungry path.

---

## Explicitly Out of Scope for Now

- **Multi-city expansion** (Phase 8 of the original plan's "market
  awareness"): the repo tracks one corridor (Virar→Andheri) in one city.
  Nothing here should assume multi-city data exists.
- **BigBasket / JioMart as data sources**: both are currently blocked
  (Akamai edge-block on BigBasket, server-side IP geolocation lock on
  JioMart per the 08-31 investigation) — cross-app comparison stays scoped
  to Blinkit/Zepto/Instamart until that changes.
- **Rating/review-based demand signals**: not available on any tracked app;
  don't budget time trying to source them.
- **Real-time or near-real-time gap refresh**: full catalog snapshots are
  ~1 store/day by design (rate-limit safety); gap detection should be judged
  on a weekly-refresh cadence, not continuous.


---

# Phase 10 — Opportunity Data Model and Persistence

## Goal

Turn the analytical output into durable, reviewable entities rather than transient HTML results.

Add logical storage for:

```text
gap_candidates
opportunities
opportunity_interpretations
opportunity_reviews
```

Suggested `opportunities` fields:

```text
opportunity_id
created_ts
updated_ts
category
gap_type
score
confidence
status
region_key
candidate_json
evidence_json
validation_json
model_metadata
```

Statuses:

```text
new
reviewing
validated
rejected
implemented
expired
```

This enables longitudinal tracking and prevents the system from rediscovering the same candidate every refresh.

---

# Phase 11 — Separate Analytical Space from Visualization Space

## Goal

Make the statistical engine independent of the 2D/3D Atlas.

Use:

```text
HIGH-DIMENSIONAL SPACE
    → nearest neighbors
    → density
    → gap detection
    → candidate scoring

2D/3D PROJECTION
    → exploration
    → interaction
    → explanation
```

The Atlas should never be the source of truth for whether a gap exists.

Persist projection artifacts separately:

```text
exports/product_space/
    semantic_2d.npy
    semantic_3d.npy
    clusters.npy
    projection_metadata.json
```

This also makes it possible to swap PCA/UMAP implementations without invalidating the opportunity database.

---

# Phase 12 — Upgrade the Atlas into Two Explicit Modes

## Goal

Keep the current Atlas and evolve it into a dual-purpose exploration surface.

## Explore mode

Answers:

> What exists?

Keep existing modes:

```text
Semantic
Price
Store
Category
Density
```

Add:

```text
Unit Price
Cross-App
```

## Opportunity mode

Answers:

> What might be missing?

Add:

```text
Gap
Opportunity
Emerging Segment
Assortment Gap
```

Use different visual glyphs:

```text
observed product      ●
candidate gap          ◎
strong opportunity    ◉
rejected               ×
```

---

# Phase 13 — Opportunity Inspector and Evidence Trail

## Goal

Make every high-ranked candidate explainable without leaving the Atlas.

Selecting an opportunity should display:

```text
Opportunity #14

Category:
Snacks → Protein Bars

Score:
84 / 100

Confidence:
A

Gap type:
Cross-app assortment + local density gap
```

Then show:

```text
Observed nearby products
Prices / unit prices
Apps carrying them
Store coverage
DPI
Coverage age
Candidate attributes
Existence checks
Validation flags
```

The inspector should have an explicit section:

```text
WHY THIS WAS FLAGGED
```

Example:

```text
1. Category coverage is sufficient.
2. Candidate region is locally sparse.
3. Neighboring products show elevated DPI.
4. Candidate configuration is absent on App X.
5. No equivalent current SKU was found.
6. Evidence is stable across recent refreshes.
```

---

# Phase 14 — Temporal / Emerging-Segment Analysis

## Goal

Use the repository's catalog history to detect changes in product space, not merely static gaps.

For each category/region track:

```text
density(t)
availability(t)
DPI(t)
assortment(t)
unit_price(t)
```

Classify:

```text
persistent gap
temporary gap
seasonal gap
emerging segment
declining segment
```

An emerging segment such as:

```text
3 SKUs → 8 → 15 → 27 → 43
```

should be surfaced separately from an empty region.

---

# Phase 15 — Candidate Stability and Robustness

## Goal

Prevent projection or parameter artifacts from becoming opportunities.

For every high-ranked candidate, compare across:

```text
PCA vs UMAP
multiple UMAP seeds
k = 5 / 10 / 20 / 50 neighborhoods
different density thresholds
recent vs historical windows
```

Store:

```text
projection_stability
neighbor_stability
temporal_stability
coverage_stability
```

Downgrade candidates that disappear under small changes.

---

# Phase 16 — Synthetic Regression Test Suite

## Goal

Prove that the engine can detect known gaps before trusting real-world results.

Create fixtures containing:

### Known assortment gap

```text
App A: product exists
App B: product exists
App C: product absent
```

### Known density gap

```text
● ● ●
● ◎ ●
● ● ●
```

### False gap caused by insufficient coverage

The system must emit:

```text
insufficient_coverage
```

and must not create an opportunity.

### Crawl artifact

Simulate a pair-count collapse and ensure it is ignored as market evidence.

### Existing product

Create a candidate that already exists under a different name and ensure it is not surfaced as new.

---

# Phase 17 — Testing and Quality Gates

Create:

```text
tests/
    test_product_fields.py
    test_product_identity.py
    test_product_space.py
    test_neighbors.py
    test_density.py
    test_gaps.py
    test_opportunities.py
    test_validation.py
```

Minimum gates:

## Product parsing

Brand and pack-size accuracy ≥ 80% on a manually checked sample.

## Cross-app identity

Manual match quality must be high enough that obvious identical products are reliably grouped.

## Coverage guard

No density-based opportunity may bypass:

```text
insufficient_coverage
stale_coverage
```

## Demand integrity

DPI remains a demand proxy, never sales volume.

## Data integrity

Vouchers remain excluded.

---

# Phase 18 — Dashboard / CLI Operationalization

The intelligence engine should become a normal part of the repo's existing operational model.

Suggested CLI progression:

```bash
python3 run.py --product-fields
python3 run.py --product-space
python3 run.py --embed-products
python3 run.py --product-neighbors
python3 run.py --product-density
python3 run.py --detect-assortment-gaps
python3 run.py --detect-gaps
python3 run.py --score-opportunities
python3 run.py --validate-opportunities
python3 run.py --opportunity-report
```

Eventually:

```bash
python3 run.py --opportunity-pipeline
```

with every stage independently rerunnable.

The dashboard should expose the same stages and continue using its existing subprocess/logging/report infrastructure.

---

# Phase 19 — Incremental Refresh Architecture

The system must not rebuild everything after every catalog sweep.

Use hashes and timestamps:

```text
new product
    → parse
    → embed
    → neighbor update

changed semantic text
    → re-embed

changed price / DPI / availability
    → market-feature refresh

unchanged product
    → skip
```

Run heavy processes periodically:

```text
full neighbor refresh
full density refresh
full gap scan
opportunity reranking
```

Given the repository's crawl cadence, a weekly opportunity refresh should be treated as the normal operating cadence until coverage materially improves.

---

# Phase 20 — LLM Interpretation, Human Review, and Learning Loop

The LLM layer should remain downstream of quantitative analysis.

## LLM input

```text
candidate
nearest products
prices
unit prices
DPI
app coverage
store coverage
coverage age
validation codes
stability
historical evidence
```

## LLM responsibilities

Generate:

```text
opportunity summary
possible customer need
possible reason for the gap
candidate differentiation
risks
validation questions
```

Never ask the model to independently invent market gaps.

## Human review

Add:

```text
Interesting
Reject
Already exists
Bad data
Needs research
Strong opportunity
```

Persist all decisions.

Only once enough human-reviewed examples exist should a learned ranking model be considered.

---

# Final Rollout Sequence

The final recommended order is:

| Milestone | Work | Output |
|---|---|---|
| M1 | Grocery product fields + union layer | Reliable normalized catalog |
| M2 | Semantic + attribute + commercial representations | Multi-layer product representation |
| M3 | Cross-app assortment gaps | First high-confidence opportunities |
| M4 | Neighbor index + density + coverage guards | Quantitative product-space structure |
| M5 | Internal/attribute gaps | Candidate sparse regions |
| M6 | DPI + churn + validation | Evidence-backed opportunity scores |
| M7 | Opportunity persistence | Longitudinal opportunity database |
| M8 | Atlas Gap/Opportunity modes | Interactive intelligence interface |
| M9 | Temporal + stability analysis | Robustness / emerging-segment signals |
| M10 | LLM + human review | Actionable opportunity briefs |
| M11 | Incremental automation | Sustainable recurring pipeline |

---

# Final Architecture

```text
                    QUICK-COMMERCE CATALOGS
                               │
                               ▼
                     NORMALIZATION / UNION
                               │
               ┌───────────────┼───────────────┐
               ▼               ▼               ▼
          PRODUCT MASTER   ATTRIBUTES       MARKET DATA
               │               │               │
               ▼               ▼               ▼
          SEMANTIC VEC     ATTRIB VEC       DPI / CHURN
               │               │               │
               └───────────────┼───────────────┘
                               ▼
                         PRODUCT SPACE
                               │
             ┌─────────────────┼─────────────────┐
             ▼                 ▼                 ▼
          NEIGHBORS         DENSITY          ASSORTMENT
             │                 │                 │
             └─────────────────┼─────────────────┘
                               ▼
                        GAP CANDIDATES
                               │
                 ┌─────────────┼─────────────┐
                 ▼             ▼             ▼
             EXISTENCE       DPI          COVERAGE
              CHECK        EVIDENCE        CHECK
                 └─────────────┼─────────────┘
                               ▼
                       OPPORTUNITY SCORE
                               │
                               ▼
                         STABILITY CHECK
                               │
                               ▼
                         HUMAN / LLM REVIEW
                               │
                               ▼
                       OPPORTUNITY DATABASE
                               │
                     ┌─────────┴─────────┐
                     ▼                   ▼
                OPPORTUNITY ATLAS    REPORTS
```

# Core design decision

The strongest implementation path is a hybrid of the two plans:

- Use the **repo-specific constraints and high-confidence assortment-gap-first rollout** from the tailored plan.
- Use the **layered semantic / attribute / commercial representation, analytical-vs-visual-space separation, opportunity persistence, stability analysis, and evidence-trail architecture** from the broader plan.

This keeps the first release grounded in what `Qcom-scraping` can actually observe today, while preserving a clean path toward the more ambitious vision of identifying genuinely underserved product configurations.

The final system should therefore make three different classes of output explicit:

```text
OBSERVED PRODUCT
ASSORTMENT GAP
POTENTIAL PRODUCT OPPORTUNITY
```

Those must never be visually or semantically conflated.
