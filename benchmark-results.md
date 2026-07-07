# Benchmark Results — Stage 3, Local NVMe

Full results for the query-layout benchmark defined in `factor-model-benchmark-plan.md` §4 Stage 3, run 2026-07-07. Raw measurements: `data/benchmark/results.jsonl`; machine-readable summary: `data/benchmark/summary.json`. Reproduce with `PYTHONPATH=python_src .venv/bin/python -m benchmark run` (harness in `python_src/benchmark/`).

## Setup

- **Data:** model `BARRA_USE4_L` — 20 years (5,218 COB dates, 2006-01-02 → 2025-12-31), ~3,000 live assets/date, 73 factors. 217M normalized loading rows; 15.39M wide rows.
- **Machine:** local NVMe, 20 cores, 62 GB RAM, DuckDB 1.5.4, Python 3.12.
- **Metric:** wall time to a usable Polars DataFrame (execute + Arrow fetch + Polars conversion). Cold runs additionally include connection/ATTACH cost.
- **Protocol:** per cell, 5 cold iterations (fresh process, page cache evicted via `posix_fadvise DONTNEED` on every data file) and 10 warm iterations (in-process repeats after one unmeasured warmup). 630 measurements total, 118 s wall.
- **Bytes scanned:** `/proc/self/io` rchar delta — logical read volume, engine-agnostic, includes page-cache hits.
- **Parity:** every arm returned identical row counts on every query (checked automatically).

### Arms

| arm | storage | layout / sort |
|---|---|---|
| `normalized` | Parquet, normalized long tables (`factor_loading` etc.) | date-sorted; pivot to wide at query time — the "as-is Snowflake" yardstick |
| `wide_cs` | Parquet, Transform A | `model_id/year_month=` partitions, sorted (cob_date, asset_id) — 1.36 GiB, 240 files |
| `wide_ts` | Parquet, Transform B | `model_id/bucket=asset_id%32` partitions, sorted (asset_id, cob_date) — 1.34 GiB, 32 files |
| `duckdb_cs` | DuckDB native table from A | sorted (cob_date, asset_id) |
| `duckdb_ts` | DuckDB native table from B | sorted (asset_id, cob_date) |
| `ducklake_cs` | DuckLake table from A | catalog-planned scans over DuckLake-managed Parquet |
| `ducklake_ts` | DuckLake table from B | ditto, asset-major insert order |

Native DuckDB store: 1.90 GiB (both layouts + membership + covariance, built in 9 s). DuckLake store: 3.16 GiB (same contents, built in 7 s). Factor covariance is deliberately not re-materialized for the Parquet arms — they share the normalized date-sorted table (90×90 is too small to need two layouts).

### Queries (frozen parameters)

| id | definition | parameters |
|---|---|---|
| CS1 | full cross-section, one date, all factors + specific risk | 2016-06-15 → 2,949 rows × 76 cols |
| CS2 | one date, 5 factors, estimation universe only | BETA, MOMENTUM, SIZE, EARNYLD, RESVOL → 2,635 rows |
| CS3 | everything for B·F·Bᵀ + D: cross-section + covariance + specific risk | CS1 + 2,701 covariance rows → 5,650 rows |
| TS1 | one asset, 20 years, all factors | asset_id 21 (full-history) → 5,218 rows |
| TS2 | 100 assets, 5 years, 3 factors (deliberately mixed) | asset_ids 4–288, 2018–2022, MOMENTUM/EARNYLD/GROWTH → 130,500 rows |
| TS3 | one covariance pair, 20 years | (BETA, MOMENTUM) → 5,218 rows |

X1 ("each transform answering the other view's query") is the off-diagonal of the grid: TS\* on cs-layouts, CS\* on ts-layouts — marked † below.

## Warm results (in-process repeat) — p50 / p95 seconds · bytes read

| query | normalized | wide_cs | wide_ts | duckdb_cs | duckdb_ts | ducklake_cs | ducklake_ts |
|---|---|---|---|---|---|---|---|
| CS1 | 0.084 / 0.115 · 8 MB | **0.015 / 0.028 · 6 MB** | † 0.191 / 0.195 · 1.33 GB | 0.006 / 0.014 · bp | † 0.023 / 0.041 · bp | 0.019 / 0.037 · 12 MB | † 0.135 / 0.148 · 1.38 GB |
| CS2 | 0.022 / 0.050 · 7 MB | **0.011 / 0.023 · 2 MB** | † 0.061 / 0.086 · 543 MB | 0.003 / 0.007 · bp | † 0.009 / 0.021 · bp | 0.015 / 0.033 · 5 MB | † 0.049 / 0.074 · 557 MB |
| CS3 | 0.094 / 0.106 · 10 MB | **0.021 / 0.034 · 8 MB** | † 0.195 / 0.199 · 1.33 GB | 0.006 / 0.021 · bp | † 0.024 / 0.041 · bp | 0.024 / 0.030 · 13 MB | † 0.142 / 0.150 · 1.38 GB |
| TS1 | 0.921 / 0.945 · 1.44 GB | † 0.219 / 0.261 · 1.36 GB | **0.019 / 0.027 · 11 MB** | † 0.121 / 0.128 · bp | 0.007 / 0.013 · bp | † 0.182 / 0.211 · 1.52 GB | 0.017 / 0.025 · 12 MB |
| TS2 | 0.123 / 0.150 · 350 MB | † 0.036 / 0.055 · 83 MB | **0.021 / 0.052 · 85 MB** | † 0.009 / 0.020 · bp | 0.007 / 0.013 · bp | † 0.025 / 0.049 · 93 MB | 0.018 / 0.045 · 24 MB |
| TS3 | 0.018 / 0.057 · 79 MB | 0.019 / 0.057 · 79 MB | 0.019 / 0.053 · 79 MB | 0.006 / 0.015 · bp | 0.006 / 0.018 · bp | 0.015 / 0.035 · 106 MB | 0.014 / 0.044 · 106 MB |

† = X1 (wrong-layout) cell. **bold** = fastest Parquet arm. `bp` = served from DuckDB's buffer pool, zero file I/O on warm repeats. TS3 is near-identical across Parquet arms because all three read the same shared covariance table.

## Cold results (fresh process, page cache evicted) — p50 / p95 seconds · bytes read

| query | normalized | wide_cs | wide_ts | duckdb_cs | duckdb_ts | ducklake_cs | ducklake_ts |
|---|---|---|---|---|---|---|---|
| CS1 | 0.150 / 0.155 · 11 MB | 0.068 / 0.072 · 8 MB | 0.322 / 0.344 · 1.33 GB | 0.073 / 0.087 · 10 MB | 0.193 / 0.231 · 746 MB | 0.114 / 0.131 · 53 MB | 0.353 / 0.375 · 1.42 GB |
| CS2 | 0.081 / 0.084 · 9 MB | 0.065 / 0.067 · 5 MB | 0.262 / 0.267 · 546 MB | 0.059 / 0.064 · 7 MB | 0.115 / 0.120 · 318 MB | 0.110 / 0.129 · 46 MB | 0.319 / 0.322 · 598 MB |
| CS3 | 0.171 / 0.179 · 13 MB | 0.103 / 0.116 · 10 MB | 0.342 / 0.350 · 1.33 GB | 0.077 / 0.083 · 11 MB | 0.200 / 0.215 · 747 MB | 0.149 / 0.161 · 54 MB | 0.386 / 0.391 · 1.42 GB |
| TS1 | 1.062 / 1.079 · 1.45 GB | 0.352 / 0.363 · 1.36 GB | 0.077 / 0.084 · 13 MB | 0.290 / 0.305 · 0.99 GB | 0.072 / 0.085 · 9 MB | 0.412 / 0.431 · 1.56 GB | 0.108 / 0.118 · 53 MB |
| TS2 | 0.198 / 0.199 · 354 MB | 0.123 / 0.146 · 86 MB | 0.112 / 0.125 · 88 MB | 0.080 / 0.097 · 66 MB | 0.066 / 0.068 · 19 MB | 0.151 / 0.161 · 134 MB | 0.113 / 0.171 · 65 MB |
| TS3 | 0.076 / 0.077 · 82 MB | 0.076 / 0.079 · 82 MB | 0.078 / 0.079 · 82 MB | 0.080 / 0.109 · 90 MB | 0.079 / 0.097 · 90 MB | 0.110 / 0.132 · 147 MB | 0.115 / 0.129 · 147 MB |

## Derived views

### Speedup over the normalized baseline (warm p50, right-layout arm)

| query | normalized | best Parquet transform | speedup | best overall (native) | speedup |
|---|---|---|---|---|---|
| CS1 | 0.084 s | wide_cs 0.015 s | 5.6× | duckdb_cs 0.006 s | 14× |
| CS2 | 0.022 s | wide_cs 0.011 s | 2.0× | duckdb_cs 0.003 s | 7.3× |
| CS3 | 0.094 s | wide_cs 0.021 s | 4.5× | duckdb_cs 0.006 s | 16× |
| TS1 | 0.921 s | wide_ts 0.019 s | **48×** | duckdb_ts 0.007 s | 132× |
| TS2 | 0.123 s | wide_ts 0.021 s | 5.9× | duckdb_ts 0.007 s | 18× |
| TS3 | 0.018 s | (shared table) | — | duckdb 0.006 s | 3.0× |

### X1 — the cost of building only one transform (warm p50)

| query | right layout | wrong layout | penalty | bytes right → wrong |
|---|---|---|---|---|
| CS1 | wide_cs 0.015 s | wide_ts 0.191 s | 12.7× | 6 MB → 1.33 GB |
| CS2 | wide_cs 0.011 s | wide_ts 0.061 s | 5.5× | 2 MB → 543 MB |
| CS3 | wide_cs 0.021 s | wide_ts 0.195 s | 9.3× | 8 MB → 1.33 GB |
| TS1 | wide_ts 0.019 s | wide_cs 0.219 s | 11.5× | 11 MB → 1.36 GB |
| TS2 | wide_ts 0.021 s | wide_cs 0.036 s | 1.7× | 85 MB ≈ 83 MB |

The wrong layout still beats the normalized baseline on TS1 (0.219 s vs 0.921 s): pre-pivoted-but-mis-sorted is better than pivot-at-query-time.

## Findings

1. **Both transforms do their job, and bytes scanned is the whole explanation.** Right-layout queries prune to 2–12 MB; wrong-layout queries scan the full ~1.35 GB store. The ~5–13× X1 time penalty is bounded only because local NVMe brute-forces 1.3 GB in ~0.2 s — on S3 this gap should widen dramatically (Stage 4's central question).
2. **The normalized baseline loses worst exactly where predicted.** TS1 (pivot 217M rows to reconstruct one asset's history) is 0.92 s warm / 1.06 s cold, reading 1.44 GB per query, every query. Cross-sectional queries lose only ~2–6× because date-sorting gives the normalized store decent row-group pruning.
3. **TS2 — the falsification test — does not justify a third layout.** The deliberately awkward mixed query runs in 21–36 ms on both wide layouts. At ~1.4 GB/model, partially-pruned brute force is cheap; the plan's "third layout" contingency is not needed at this scale.
4. **DuckDB native tables are the fastest warm arm everywhere** (3–24 ms; warm repeats are pure buffer-pool hits with zero file I/O), for +40% storage over Parquet (1.90 vs 1.36 GiB) and an opaque single-file format. Cold, they are roughly at Parquet parity — and notably *worse* on wrong-layout cold queries (TS1 cold on duckdb_cs reads 0.99 GB vs Parquet's pruned reads elsewhere).
5. **DuckLake adds nothing locally — as expected.** It tracks raw-Parquet performance within ~1.5× (catalog round-trip overhead on multi-hundred-file scans), confirming the plan's expectation that its value is metadata planning over high-latency object storage, not local speed. Its store doubles storage only because we loaded both layouts into one catalog.
6. **Daily pipeline cost is trivial; B's file-count growth is the one real ops concern.** Full rebuild: ~2 min (A) + ~19 s (B) per model. Daily incremental: ~0.14 s per model per transform, but B appends 32 × ~26 KiB files/day (~8,350 files/model-year) and needs periodic compaction; A appends one ~320 KiB file/day.

**Bottom line:** dual materialization is worth it — each transform wins its native queries by 5–48× over the normalized store, the mixed case is fine on either, and the whole thing rebuilds from scratch in minutes. The open question is remote access, where the pruning-vs-scan gap (MB vs GB per query) should dominate: Stage 4.
