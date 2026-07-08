# Generator Spec v2 — Fleet, Global Scale, Returns & FMPs

Extends `generator-spec.md` (v1, built and validated) per practitioner review
(a colleague, SFM, 2026-07-08). **Purpose of this document: pin every
dimension as a number a practitioner can eyeball and correct before we
generate.** Numbers marked ❓ are the explicit validation asks.

All v1 mechanics carry over unchanged: deterministic Philox seed scheme,
AR(1) persistence, cross-sectional standardization, sparse loading storage,
7-significant-digit rounding, PSD covariance construction, per-model
conventions, year-boundary checkpoints.

---

## 1. Model fleet

**Ten vendor models** (count confirmed by review follow-up, 2026-07-08: expect
5–10; we assume 10) + two customization variants. ❓ *Are the per-model
dimensions and the region/vendor mix sane?*

| model_id | proxy for | style | industry | country | currency | market | **factors** | coverage | estu |
|---|---|---|---|---|---|---|---|---|---|
| `AX_WW4_MH` | Axioma global (AXWW4-MH) | 16 | 64 | 95 | 72 | 1 | **248** | 58,000 | 13,000 |
| `BARRA_GEM_L` | Barra global (GEM-like) | 12 | 60 | 90 | 70 | 1 | **233** | 50,000 | 11,000 |
| `BARRA_USE4_L` | Barra US (v1, as built) | 12 | 60 | — | — | 1 | **73** | 3,000 | 2,700 |
| `AX_US4_MH` | Axioma US (v1, as built) | 13 | 68 | — | — | 1 | **82** | 3,000 | 2,700 |
| `BARRA_EUE4_L` | Barra Europe | 12 | 50 | 24 | 12 | 1 | **99** | 6,000 | 2,500 |
| `AX_EU4_MH` | Axioma Europe | 13 | 45 | 24 | 12 | 1 | **95** | 6,000 | 2,500 |
| `BARRA_JPE4_L` | Barra Japan | 12 | 30 | — | — | 1 | **43** | 4,000 | 1,800 |
| `AX_JP4_MH` | Axioma Japan | 13 | 33 | — | — | 1 | **47** | 4,000 | 1,800 |
| `BARRA_EME4_L` | Barra Emerging | 12 | 38 | 38 | 26 | 1 | **115** | 8,000 | 3,000 |
| `AX_EM4_MH` | Axioma Emerging | 13 | 40 | 40 | 28 | 1 | **122** | 8,000 | 3,000 |
| `AX_WW4_MH_SFM1` | custom variant | +3 custom styles on `AX_WW4_MH` | | | | | **251** | 58,000 | 13,000 |
| `BARRA_USE4_L_SFM1` | custom variant | +2 custom styles on `BARRA_USE4_L` | | | | | **75** | 3,000 | 2,700 |

- Global reference point from review: AXWW4-MH = 248 factors incl. 72 currency
  + 95 country; estu ~13,000; coverage ~58,000. ❓ *Style/industry split of the
  remaining 81 factors — is 16 style / 64 industry right?*
- **Variant mechanics** (the "model customization" requirement): a variant is a
  **new model_id** referencing a base — generated as the base taxonomy ± a
  factor delta, full data materialized (that materialization cost is a thing
  we measure, not avoid). Variant custom styles are fresh AR(1) series; all
  shared factors are byte-identical to the base by seed construction.
  ❓ *Cadence assumption: a variant appears ~quarterly and lives for years —
  reasonable?*
- Universes: each region has its own asset superset with 5%/yr churn (v1
  mechanics). Global models cover the union of regional universes plus a
  rest-of-world pool. Multi-listed securities: an asset has exactly one
  country and one currency loading (both 1.0). ❓ *OK to ignore multi-currency
  lines / DR-vs-ordinary linkage for a storage benchmark?*

### Nonzero loadings per asset-date (drives all sizing)

| model class | styles | industry | country | currency | market | ≈ rows/asset/day |
|---|---|---|---|---|---|---|
| global | 16 (dense) | 1–2 | 1 | 1 | 1 | **~20** |
| US regional | 12–13 (dense) | 1–2 | — | — | 1 | **~15** |
| EU/JP regional | 12–13 (dense) | 1–2 | 0–1 | 0–1 | 1 | **~16** |

---

## 2. New datasets (from review: part of the daily load)

### factor_return

```sql
factor_return(model_id, cob_date, factor_id, value, version_id)
```

One row per factor per day. Generation: `r_t = vol_f/√252 · z_t` with mild
AR(1) on `z` (φ=0.1) so cumulative series look sane; vol_f = the factor's v1
vol target. Convention per model (`return_convention` in `model_master`):
daily decimal (Axioma-style) vs daily percent (Barra-style). Rows: n_factors ×
5,218/model — trivial (< 10M total). ❓ *Convention check: daily local-currency
returns, no compounding within a day?*

### fmp — factor-mimicking portfolios

```sql
fmp(model_id, cob_date, factor_id, asset_id, weight, version_id)
```

Per review: (style + market factors) × assets, daily. Pinned as: **weights
over the estimation universe, dense** (every estu asset gets a weight per
style/market factor), weights ~ N(0, 1/n_estu) normalized to unit gross per
factor, AR(1) over time (φ=0.97) so portfolios turn over slowly.
❓ *Estu-only and dense — right? Or coverage-wide / sparse thresholded?*

| model | (styles+1) × estu × 5,218 | rows/20y |
|---|---|---|
| AX_WW4_MH | 17 × 13,000 × 5,218 | **1.15B** |
| BARRA_GEM_L | 13 × 11,000 × 5,218 | 0.75B |
| eight regionals + variants | | ~1.4B |
| **total** | | **~3.3B rows ≈ 26 GB** |

---

## 3. Storage-strategy arms (the table-per-model question)

The review's operational criterion: *adding a new model must never become a
project.* Three physical strategies, all fed from the same normalized store,
all benchmarked — plus an **operational drill** (add `AX_JP4_MH` as a new
model, then add a variant) scored on wall-time, steps, and bytes rewritten.

**(A) Per-model wide tables** — v1 as built. DDL is *generated* from
`factor_master`, so a new model costs one config entry and a transform run;
no hand-written schema. N models ⇒ N tables (× 2 layouts).

**(B) Generic-slot wide table** — one physical table for all models:

```sql
wide_generic(
    model_id VARCHAR, cob_date DATE, asset_id INTEGER, version_id SMALLINT,
    specific_risk DOUBLE,
    F001 DOUBLE, F002 DOUBLE, ... F260 DOUBLE      -- slot pool ≥ max model + headroom
)
factor_slot_map(
    model_id VARCHAR, slot_id SMALLINT, factor_id VARCHAR, factor_seq SMALLINT,
    valid_from DATE, valid_to DATE                  -- temporal: model revisions remap
)
-- generated, the ONLY human query surface:
CREATE VIEW wide_cs_barra_use4_l AS
  SELECT cob_date, asset_id, F001 AS "BETA", F002 AS "MOMENTUM", ..., specific_risk
  FROM wide_generic WHERE model_id = 'BARRA_USE4_L';
```

Rules that make (B) safe: slot assignment is **append-only per model** (a slot
id is never reused across a model's revisions); raw `Fnnn` columns are never
exposed — humans and tools query the generated views, so pointing at the wrong
model still fails loudly (missing named column) instead of silently returning
another factor's numbers. Partition by `model_id` first, so scan behavior per
model matches (A); unused slots are NULL (near-free in Parquet — definition
levels only) but widen footers/stats: a regional model reads a 265-column
schema to use 80 — one of the things to measure. A float-only slot pool
suffices for this payload; typed pools (`Cn_Int`, `Cn_VarChar`) are the same
pattern generalized, only needed if heterogeneous attributes join the table.

**(C) Normalized long** — v1 baseline; zero DDL forever; pivot cost already
quantified (5–48× on right-layout queries).

Expected outcome (to be tested, not assumed): (A) ≈ (B) on query performance
within a model partition; (B) wins the add-a-model drill outright (insert
slot-map rows + create views — no data movement, no new tables); (A)'s
generated-DDL approach is close behind; (C) wins the drill trivially but loses
the query suite. The decision then reduces to (B)'s governance overhead vs
(A)'s object-count growth at 10+ models × 2 layouts × variants.

---

## 4. Query suite additions

| id | pattern (from review: sessions run chains) |
|---|---|
| CHAIN1 | loadings for a security set (100 names, latest date) → factor returns for those factors, 5y |
| CHAIN2 | loadings (one date, estu) → factor covariance → specific risk (full risk-model pull) |
| FMP1 | one factor's mimicking portfolio, one date (weights vector) |
| FMP2 | one factor's FMP weights for 10 assets, 20y (time-series view of FMPs) |
| DRILL | operational: add a model; add a variant (wall-time, steps, bytes) |

Chains are timed end-to-end per session (connect → last DataFrame), cold and
warm — fixed costs weighted the way real usage weights them.

---

## 5. Sizing (20 years, 5,218 dates) and tiers

Variants are fully materialized and included below — the global variant alone
is ~7.0B loading rows, which is precisely the customization cost worth
measuring. ❓ *Is global-model customization actually expected, or does it
happen only on regionals? (Changes totals by ~35%.)*

| dataset | rows | ≈ compressed |
|---|---|---|
| factor_loading, 12 models | **~21.0B** (two globals 10.3B; global variant 7.0B) | ~150 GB |
| fmp | ~4.9B | ~38 GB |
| specific_risk | ~1.1B | ~9 GB |
| factor_covariance | ~0.5B | ~4.5 GB |
| factor_return + dims | ~15M | ~0.1 GB |
| **normalized total** | | **~200 GB** |
| wide transforms (dual, per strategy arm) | | +170–250 GB |
| **project total** | | **~0.45–0.6 TB** |

- **Dev tier**: regionals + one variant only — v1 scale (~15 GB), laptop-friendly.
- **Full tier**: everything, generated on EC2 (r7gd.2xlarge, est. 2–4 h), lives
  in S3 (~$8–10/mo at 0.35 TB). Same deterministic seeds ⇒ both tiers agree
  exactly where they overlap.
- Restatements (promoted from v1 deferred): inject `version_id = 2` rows for a
  seeded 1% of (model, date) pairs at T+1..T+5 lag; as-of query forms added to
  the suite. ❓ *Realistic restatement rate/lag?*

---

## 6. Open validation questions (consolidated)

1. Global factor split: 16 style / 64 industry / 95 country / 72 currency / 1 market for the AXWW4-MH proxy?
2. Coverage 58k / estu 13k global; regional universes per §1 table?
3. FMPs: estu-only, dense weights, styles + market only?
4. Factor returns: daily, local currency, vendor-convention units?
5. ~~Fleet size~~ **confirmed: 10 vendor models.** Still open: variant cadence (~quarterly?).
6. Restatement rate ~1% of model-dates, lag ≤ 5 days?
7. Anything material missing from the daily load besides returns/FMPs (e.g., descriptor-level data, currency risk-free curves)?
8. Do customizations happen on global models, or regionals only? (A materialized global variant is ~7B loading rows — it moves the totals by ~35%.)
