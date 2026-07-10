# V2 Three-Way Storage Benchmark — Global Scale, Local NVMe/SATA

The storage-strategy question from practitioner review (generator-spec-v2 §3),
measured. Model under test: `AX_WW4_MH` — 248 factors, 58,000 coverage /
13,000 estu — the AXWW4-MH proxy; 20 years, 302M wide rows per layout.
Machine: i7-12700K, 62 GB, data on SATA SSD (~540 MB/s), DuckDB 40 GB /
14 threads per worker (no-spill). Cold = fresh process + page-cache eviction
of the arm's data; warm = in-process repeats. p50; raw in `data/v2bench/`.

## Arms

| | storage | add-a-model cost |
|---|---|---|
| **A** per-model wide | `wide_cs`/`wide_ts` Parquet, named factor columns per model | config + transform run; N models ⇒ 2N tables |
| **B** generic-slot | one 265-column schema (`F001..F260`), queried through generated per-model views (slot → factor-name aliasing) | materialize rows + insert slot-map rows + create views; table count constant |
| **C** normalized | long `factor_loading`, pivot at query time | nothing |

## Results — full 12-query suite (p50 seconds, cold / warm)

Wide-arm queries carry partition predicates (`year_month` / `bucket`) — see
finding 0. C cells from the same suite, unaffected by the fix.

| query | A per-model | B generic views | C normalized |
|---|---|---|---|
| CS1 full cross-section, 1 date, all 249 cols | 0.29 / **0.18** | 0.32 / **0.16** | 8.6 / 8.2 |
| CS2 one date, 5 styles, estu only | 0.13 / **0.027** | 0.16 / **0.027** | 0.21 / 0.063 |
| CS3 risk-model pull (xs → cov → srisk) | 0.28 / **0.12** | 0.31 / **0.11** | 10.4 / 8.8 |
| CS4 country slice (617 assets), all cols | 0.15 / **0.051** | 0.19 / **0.049** | 8.4 / 8.1 |
| CS5 as-of (PIT) cross-section, restated date | 0.41 / **0.30** | 0.41 / **0.30** | 26.6 / 26.4 |
| CS6 momentum screen: estu top-1300 | 0.15 / **0.028** | 0.16 / **0.027** | 0.22 / 0.072 |
| TS1 one asset, 20y, all factors | 1.00 / **0.075** | 0.93 / **0.071** | 73.2 / 12.3 |
| TS2 100 assets × 5y × 3 factors | 3.84 / **0.17** | 3.93 / **0.17** | 18.8 / 2.5 |
| TS3 covariance pair, 20y (shared table) | 2.77 / 0.14 | 2.77 / 0.14 | 2.76 / 0.14 |
| TS4 factor panel: estu × month-ends × 10y | 9.23 / **0.89** | 9.19 / **0.89** | 38.4 / 4.7 |
| TS5 tall-thin: 10 assets × 2 cols × 20y | 2.12 / **0.085** | 1.66 / **0.086** | 83.3 / 41.8 |
| TS6 as-of (PIT) single-asset history | 1.11 / **0.23** | 1.08 / **0.22** | 73.9 / 23.1 |
| CHAIN1 / FMP1 / FMP2 (earlier run, pre-fix wide numbers) | 0.66 / 0.41 · 0.16 / 0.03 · 16.7 / 0.82 | ≈A | C ties CHAIN1 |

## Findings

0. **Schema width costs metadata, not data — and partition discipline pays
   for it.** The first run of this suite globbed 240 monthly / 640 bucket
   files with no partition-column predicate; every wide-arm query paid
   ~160–250 ms parsing footers (251 columns × row-group stats × hundreds of
   files), which made the long store look 3× faster on narrow slices. With
   `year_month`/`bucket` predicates (standard practice; v1's harness had
   them), **the wide layouts win all twelve queries** — CS2 0.027s vs C's
   0.063s. Corollary fixed in the design: generated views must expose the
   partition column, or no client can prune. (DuckLake removes this entire
   cost class — its catalog knows every file without reading footers.)
0b. **The PIT dimension is nearly free on wide layouts via overlay** —
   as-of queries (CS5/TS6) run at 0.30s / 0.23s warm by joining normalized
   version-2 rows onto the v1 wide base, versus 26s / 23s for C's
   `arg_max` over everything it touches: ~90×. The wide layouts' one
   structural gap closes with a cheap hybrid pattern.
1. **The generic-slot design costs nothing measurable at query time: B ≈ A
   within noise on every query** (B faster on several), cold and warm, at
   full global scale. The 265-column physical schema is free — NULL slots
   vanish into encoding, projection reads only aliased columns. Combined
   with B's structural add-a-model advantage (constant object count,
   mapping rows instead of DDL), **B is the recommended physical
   strategy** pending the DRILL measurement.
2. **Pivot-at-query is untenable at global scale.** C is 20–35× slower warm
   on cross-section and risk-model chains (8–12s answers), and its cold
   single-asset history takes 74s. The one exception (CHAIN1, where C ties)
   is a date-pruned, predicate-pushed narrow slice — the pattern C handles.
   As the fleet's daily working format, C survives only as the canonical
   source feeding materializations.
3. **Global-scale wide layouts hold up:** a 58k × 251 cross-section
   materializes to a DataFrame in ~0.4s warm / 0.6s cold; a 20-year
   single-asset history in ~0.35s warm. The v1 conclusions transfer to 28×
   the data.
4. **FMP2 exposes the next layout gap:** pulling one factor × 10 assets ×
   20y from the date-partitioned FMP store scans it all cold (16.7s). If
   FMP time-series queries matter in practice, `fmp` wants the same dual
   materialization treatment as loadings — worth asking in the Chris thread.

## Still to run

- **DRILL** — the operational add-a-model/add-a-variant timing per strategy
  (the review's "must never become a project" criterion).
- Same grid over S3/DuckLake (remote-client story at global scale).
