# Equity Factor Model Data Generator & Query Performance Benchmark — Plan

**Goal:** Generate 20 years of synthetic Barra/Axioma-style equity factor model data in a normalized schema, build two query-optimized transformations (cross-sectional and time-series), and benchmark query performance across engines — locally first, then on AWS.

---

## 1. Design Decisions and Parameters

### Universe and factors
- Model on a Barra USE4 / Axioma US-style setup: **3,000 assets** (Russell 3000-like), **90 factors**.
- Factor taxonomy matters even for synthetic data:
  - ~10 style factors (dense float loadings)
  - ~60 industry factors (mostly zero / one-hot; an asset loads on 1–2 industries)
  - ~15 country/currency factors (or collapse to style + industry for single-country)
  - 1 market factor
- Real loadings tables are therefore **~70% sparse**. This drives compression and scan behavior. A generator emitting 90 dense floats per asset overstates storage and understates scan speed relative to production.

### Universe churn
- Do **not** use a static 3,000 names for 20 years. Real universes turn over ~5–8%/year.
- Generate a superset of **~6,000 asset IDs** with entry/exit dates so any given COB has ~3,000 live names.
- Exercises the painful real-world queries: single-asset history where the asset exists for only part of the window; cross-sections with date-varying membership.

### Temporal persistence — the most important generator decision
- Columnar query performance is dominated by compression; compression is dominated by data entropy.
- Real loadings are highly autocorrelated. **i.i.d. random noise is incompressible** and produces pessimistic, unrepresentative results.
- Generate:
  - Style loadings as AR(1): `x_t = 0.99 · x_{t-1} + ε`
  - Industry memberships near-static
  - Covariance matrices evolving slowly (perturb a base matrix)

### Statistical realism beyond that: don't bother
- No need for a valid risk model. Just ensure covariances are positive semi-definite (generate via `A·Aᵀ` from a random factor structure, then perturb) so downstream math doesn't blow up.

### Scale check
| Table | Rows over 20 years (~5,040 trading days) |
|---|---|
| Factor loadings | 3,000 × 90 × 5,040 ≈ **1.36B values** |
| Factor covariance (upper triangle) | 4,095/date ≈ 20.6M rows |
| Specific risk | 3,000/date ≈ 15M rows |

The loadings table is the only thing that matters for performance; everything else comes along for free.

---

## 2. Schemas

### Multi-model design (Barra + Axioma in one store)

Every fact table carries a `model_id`, but the design principles matter more than the column:

1. **Factor identity is model-scoped.** Barra "MOMENTUM" and Axioma "MT_MOMENTUM" are different factors — different definitions, horizons, estimation universes. `factor_id` is only meaningful as `(model_id, factor_id)`; the composite key is the PK on `factor_master`. Never unify taxonomies in the core schema. Cross-model comparison, if ever needed, is an optional `factor_xref(model_id_a, factor_id_a, model_id_b, factor_id_b, mapping_quality)` layered on top and explicitly approximate.
2. **Asset identity is firm-scoped; vendor IDs are cross-referenced.** Internal `asset_id` plus `asset_xref(asset_id, vendor, vendor_asset_id)` mapping to Barra IDs / Axioma IDs (same pattern as OpenFIGI mapping).
3. **Vendor conventions live in `model_master`.** Covariance scaling (annualized variance % vs daily variance), specific risk convention (annualized vol % vs daily vol), currency. Mixing these silently is where real cross-vendor bugs come from — a Barra specific risk of `15.77` and an Axioma `0.0152` can describe the same asset.
4. **A model revision is a new `model_id`** (USE4 → USE5, AXUS4 → AXUS5), never an in-place mutation of factor definitions.
5. **Wide transforms are per-model.** A Barra-like model pivots to ~73 factor columns, an Axioma-like to ~82 with different names. One unioned wide table means nulls everywhere and schema churn on every model revision. `model_id` becomes the leading partition directory; each model gets its own wide schema beneath it.

### Normalized (the "as-is Snowflake" baseline)

```sql
model_master(model_id, vendor, model_name, variant, region, n_factors,
             cov_scaling, specific_risk_convention)
factor_master(model_id, factor_id, factor_name, factor_type)        -- PK (model_id, factor_id)
asset_master(asset_id, ticker, sector, country, start_date, end_date)
asset_xref(asset_id, vendor, vendor_asset_id)
factor_loading(model_id, cob_date, asset_id, factor_id, value, version_id)
factor_covariance(model_id, cob_date, factor_id_1, factor_id_2, value, version_id)
specific_risk(model_id, cob_date, asset_id, value, version_id)
universe_membership(model_id, cob_date, asset_id, estimation_universe_flag)
```

Note `universe_membership` is also model-keyed: Barra and Axioma coverage universes differ, and each model's estimation universe is its own concept.

### Demonstrated with two generated models

A working demonstration (`multi_model_demo.py`) generates both into one normalized store:

| model_id | vendor | styles | industries | market | total factors | cov scaling | specific risk |
|---|---|---|---|---|---|---|---|
| BARRA_USE4_L | MSCI Barra | 12 (BETA, MOMENTUM, SIZE, EARNYLD, RESVOL, GROWTH, BTOP, LEVERAGE, LIQUIDTY, SIZENL, DIVYLD, SENTMT) | 60 (IND01–IND60) | COUNTRY | 73 | annualized variance % | annualized vol % |
| AXIOMA_US4_MH | SimCorp Axioma | 13 (MARKET_SENSITIVITY, MT_MOMENTUM, ST_MOMENTUM, SIZE, VALUE, GROWTH, LEVERAGE, LIQUIDITY, VOLATILITY, EXCHANGE_RATE_SENS, DIVIDEND_YIELD, PROFITABILITY, EARNINGS_YIELD) | 68 (SEC01–SEC68) | MARKET | 82 | daily variance | daily vol |

The demo shows: one `factor_loading` table holding both models' rows; the same internal asset carrying a Barra ID and an Axioma ID via `asset_xref`; and the pivot generating **two wide tables with different schemas** — `wide_cs_barra_use4_l` (12 rows × 76 cols) and `wide_cs_axioma_us4_mh` (12 rows × 85 cols) — with the column list driven entirely from `factor_master`, so adding a third model requires zero schema code.

### Generator implication

The generator's parameter block becomes a **list of model configs** (styles, industry count, market factor, conventions, AR coefficients), and the pipeline loops per model. Data volume scales linearly per model added: two models ≈ 2.7B loading values over 20 years — still laptop-scale.

- Include `version_id` even though generation produces version 1 everywhere. It makes the schema honest about the restatement dimension and allows later injection of restatements to test bitemporal query cost.

### Transform A — cross-sectional (date-major)
- Wide: one row per `(cob_date, asset_id)` with the model's named factor columns plus `specific_risk`.
- Directory layout: `model_id=<M>/year_month=<YYYY-MM>/...` — model is the leading partition, then **month** (5,040 daily partitions is small-file territory; monthly ≈ 63,000 rows/partition is better Parquet granularity), sorted by `cob_date` within partition.
- A cross-sectional query touches one partition and prunes to the requested factor columns.

### Transform B — time-series (asset-major)
- Same wide row shape, opposite physical order.
- Directory layout: `model_id=<M>/bucket=<hash(asset_id) % N>/...` (N ≈ 32–64), sorted `(asset_id, cob_date)` within.
- A single-asset 20-year pull touches one bucket and reads a contiguous run.

### Shared
- Covariance matrices: single date-sorted table shared by both transforms — 90×90 is too small to need two layouts.
- Deliberately absent: one table serving both views. That's the existing Snowflake compromise; the experiment measures what dual materialization buys.

---

## 3. Formats and Engines

### Interchange format
- **Parquet everywhere**: zstd compression, row groups ~128MB, column statistics on.
- Common denominator every engine reads; the honest way to compare engines on identical bytes.

### Benchmark arms
1. **DuckDB over normalized Parquet** — reproduces the Snowflake pain locally. Expected to lose badly; that's the yardstick.
2. **DuckDB over Transform A and B Parquet** — the main event. Hive-partitioned directories, `read_parquet` with filter pushdown.
3. **DuckDB native tables** (both sort orders) — measures what DuckDB's own storage format adds over raw Parquet.
4. **DuckLake over the transforms** — same Parquet plus metadata catalog. Measures snapshot-resolution overhead/benefit; gives the versioned variant essentially free.
5. **ArcticDB** (optional arm) — wide panel keyed by date; `read` with date-range + column selection for TS, per-date reads for CS, Arrow output. Tests whether the purpose-built DataFrame store beats the SQL engine on pure retrieval.

### Query surface
- Everything returns **Arrow**, then Polars/pandas — Python research is the consumer.
- **Metric: time to usable DataFrame**, not engine-internal query time.

---

## 4. Pipeline

### Stage 1 — Generate
- Python/NumPy, chunked by year to bound memory.
- Natural generation shape is wide (per date: 3,000×90 loadings array, 90×90 covariance). Generate wide, **derive normalized by melting**, and write normalized Parquet as the canonical output — part of the test is the cost of building transforms *from* the normalized form (the real migration path from Snowflake).
- **Deterministic seed per date** so any slice is reproducible; restatements can be injected later as seeded perturbations.
- Runtime estimate: minutes — 1.36B values of vectorized NumPy is nothing.

### Stage 2 — Transform
- Pure DuckDB SQL from normalized Parquet:
  - `PIVOT` (or explicit 90-way conditional aggregation — faster and predictable) → Transform A.
  - Re-sort/re-partition pass → Transform B via `COPY ... TO ... (PARTITION_BY ...)`.
- **Record transform wall-time and output size** — this is the future daily-pipeline cost.
- Daily incremental = same SQL filtered to one date: appends one partition (A); rewrites N bucket tails or appends per-bucket files (B). **B's incremental append is the awkward one — measure it explicitly.**

### Stage 3 — Benchmark harness
- Parameterized query suite; each run cold (fresh process, page cache dropped where possible) and warm; ~10 iterations; report **p50/p95 and bytes scanned**.

| ID | Query |
|---|---|
| CS1 | Full cross-section, one date, all factors |
| CS2 | One date, 5 factors, estimation universe only |
| CS3 | Cross-section joined to covariance + specific risk (everything needed for `B·F·Bᵀ + D` for one date) |
| TS1 | One asset, 20 years, all factors |
| TS2 | 100 assets, 5 years, 3 factors (mixed case — deliberately awkward for both layouts) |
| TS3 | Factor covariance time series, one factor pair, 20 years |
| X1 | Each transform answering the *other* view's query — quantifies the cost of building only one |

### Stage 4 — AWS
- Same Parquet/DuckLake data pushed to **S3**; DuckDB on Graviton EC2 (r7g/c7g) via httpfs.
- DuckLake catalog moves from local DuckDB file to **Postgres (RDS)** for multi-reader.
- Re-run the identical harness. Deltas measured: S3 latency/throughput vs local NVMe; value of partition pruning when every miss is a network round trip.
- Two S3-specific experiments:
  - **S3 Express One Zone** for hot recent partitions.
  - **Row-group sizing** — S3 favors fewer, larger reads than local disk.
- Optional later: load the same Parquet into Snowflake via external stage for a direct comparison.

### Stage 4b — Laptop client access (remote research pattern)

How a laptop queries the S3 data efficiently, in escalating order of machinery:

**Direct DuckDB httpfs (baseline).** DuckDB never downloads whole files — it issues HTTP range requests: Parquet footer first (schema + row-group stats), then only the byte ranges for columns surviving projection and row groups surviving predicate pushdown. With Hive partitioning, a CS2-type query (one date, 5 factors) is ~5–15 requests at ~50–100ms RTT — sub-second, a few MB moved. Consequence: **partition and row-group sizing matter more on S3 than locally** — every miss is a network round trip, and chatty layouts (thousands of tiny row groups, daily partitions) die on latency, not bandwidth.

**Degradation modes:** file listing/globbing over many partitions; repeated footer reads across sessions; non-prunable mixed queries (TS2 shape).

**Mitigations:**
1. **DuckLake catalog (Postgres/RDS) fixes metadata structurally.** One SQL round trip to the catalog to plan (it knows every file, partition values, column stats), then range-reads of exactly the needed data files. No listing, no footer discovery. On local NVMe DuckLake is overhead; from a laptop against S3 it is the difference-maker — a strong argument for that benchmark arm.
2. **Local range caching:** DuckDB's `cache_httpfs` extension persists fetched ranges to local disk; repeated sessions against the same recent months stop touching S3 after first read.
3. **Sync, don't query (the blunt option this data size unlocks):** each wide transform is ~3–6GB/model compressed. `aws s3 sync` the whole thing (or trailing N years) to laptop NVMe in minutes; an etag/manifest check on session start keeps it current (steady-state delta = one date-partition/day). For a small team this is often strictly better than clever remote reads: zero latency sensitivity, works offline, S3 costs round to zero.

**Deliberately deferred:** a server-side query tier (EC2 + Arrow Flight SQL, laptop as thin client). Right shape when data outgrows laptops or heavy joins must run near the data; at ~30GB total it is infrastructure without payoff and reintroduces the shared-compute-box ops burden the embedded approach avoids.

**Benchmark addition:** run the harness laptop-over-S3 alongside the EC2 runs. Client→bucket RTT dominates (e.g. Ireland → eu-west-1 vs us-east-1); bucket region is a one-line choice worth getting right early.

---

## 5. Sizing Expectations and Falsification Criteria

Rough compressed footprints, given AR(1) persistence:

| Dataset | Estimated size |
|---|---|
| Normalized Parquet | ~8–14 GB (repeated keys dictionary-encode well, but 1.36B rows of key overhead is the tax being measured) |
| Each wide transform | ~3–6 GB |
| **Total project** | **< 30 GB** — laptop-scale; local-first is the right sequencing |

**Result that would change the plan:** if TS2-style mixed queries dominate real research patterns and neither transform wins them, the answer becomes either a third layout (monthly partitions sorted by asset within) or accepting the wide date-major copy plus DuckDB parallel scan brute-forcing it — at ~5 GB total, "scan everything" is often sub-second anyway.

---

## Next Step

Draft the **generator spec**: parameter block (universe size, churn rate, factor taxonomy, AR coefficients, seed scheme) plus the normalized DDL. Everything downstream is mechanical once that's pinned.

---

## V2 Scope — practitioner review (a colleague, SFM, 2026-07-08)

V1 (everything above) is built, benchmarked across four environments, and its conclusions stand. Chris's review of the plan/spec surfaced four gaps between the proxy and the real daily load; v2 closes them. Full dimensioning in `generator-spec-v2.md` — written so he can validate the numbers before we generate.

1. **Missing datasets: factor returns and factor-mimicking portfolios.** Both are part of the daily vendor load. v2 adds `factor_return` (n_factors × 1 per day — trivially small) and `fmp` ((style + market factors) × estimation-universe assets per day — decidedly not small at global scale: ~1.15B rows/20y for one global model).

2. **Global models are a different size class.** V1's dimensions are regional. v2 adds global-model configs proxying AXWW4-MH: **248 factors** (16 style / 64 industry / 95 country / 72 currency / 1 market), **~58,000 coverage / ~13,000 estimation universe**. One global model's loadings ≈ **6.1B rows/20y** — ~28× a v1 regional model. This moves the project from "laptop-scale" to "generate on EC2, two tiers": a dev tier (regionals only, v1-scale) and a full tier (~0.3 TB total).

3. **Model fleet + customization.** Real usage is 5–10 vendor models plus **custom variants** (add/remove factors ⇒ new model variant), with a hard requirement that *adding a model must never be a project*. v2 specs a 6-model fleet (2 global, 4 regional) + 2 variants, and — the design question this raises — makes **storage strategy a measured three-way decision** rather than an opinion:
   - **(A) per-model wide tables** (v1's approach; DDL generated from `factor_master`, so "setup" is config, not code);
   - **(B) one generic-slot wide table** — fixed positional columns `F001…F260 DOUBLE` + core keys, with a `factor_slot_map` aliasing slots to model-scoped factor names, and **generated per-model views as the only human query surface** (raw slot columns are never queried directly — the view layer restores loud failure on wrong-model access);
   - **(C) normalized long** (v1's baseline — zero DDL forever, pivot cost already measured).
   Each arm is scored on the query suite **plus an operational drill: add a new model and a new variant — wall-time, steps, bytes rewritten.** That drill is the criterion Chris actually stated.

4. **Serial query chains.** Research sessions run chains, not isolated queries: loadings → factor returns; loadings → covariance → specific risk. v2 adds CHAIN1/CHAIN2 to the suite, measured end-to-end per session (cold and warm) — this weights per-query fixed costs (connection, catalog, first-touch) the way real usage does.

Also promoted from v1's deferred list, since PIT behavior is part of the daily-load story: **restatement injection** (`version_id ≥ 2`) with as-of query forms.
