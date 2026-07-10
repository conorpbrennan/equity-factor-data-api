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

| query | A per-model | B generic views | C normalized |
|---|---|---|---|
| CS1 full cross-section, 1 date, all 249 cols | 0.63 / **0.39** | 0.70 / **0.41** | 8.6 / 8.2 |
| CS2 one date, 5 styles, estu only | 0.39 / 0.19 | 0.43 / 0.21 | 0.21 / **0.06** |
| CS3 risk-model pull (xs → cov → srisk) | 0.78 / **0.51** | 0.87 / **0.55** | 10.4 / 8.8 |
| CS4 country slice (617 assets), all cols | 0.79 / **0.31** | 0.86 / **0.33** | 8.4 / 8.1 |
| CS5 as-of (PIT) cross-section, restated date | 0.84 / **0.59** | 0.86 / **0.60** | 26.6 / 26.4 |
| CS6 momentum screen: estu top-1300 | 0.37 / 0.25 | 0.42 / 0.19 | 0.22 / **0.07** |
| TS1 one asset, 20y, all factors | 1.52 / **0.35** | 1.43 / **0.35** | 73.2 / 12.3 |
| TS2 100 assets × 5y × 3 factors | 3.86 / **0.18** | 3.91 / **0.18** | 18.8 / 2.5 |
| TS3 covariance pair, 20y (shared table) | 2.77 / 0.14 | 2.77 / 0.14 | 2.76 / 0.14 |
| TS4 factor panel: estu × month-ends × 10y | 9.34 / **0.97** | 9.31 / **0.98** | 38.4 / 4.7 |
| TS5 tall-thin: 10 assets × 2 cols × 20y | 4.16 / **0.19** | 3.16 / **0.19** | 83.3 / 41.8 |
| TS6 as-of (PIT) single-asset history | 1.69 / **0.52** | 1.61 / **0.52** | 73.9 / 23.1 |
| CHAIN1 100 names → returns 5y (earlier run) | 0.66 / 0.41 | 0.66 / 0.43 | 0.59 / 0.34 |
| FMP1 / FMP2 (shared store, earlier run) | 0.16 / 0.03 · 16.7 / 0.82 | = | = |

## Findings

0. **Across all twelve queries, B ≈ A within noise (±6%, and B occasionally
   faster)** — the full suite confirms the original four-query result. The
   long store wins exactly two cells: CS2 and CS6, the narrow-projection
   one-date queries (3× — reading 5 factors from long format touches far
   less data than a 249-column wide row). Everything else belongs to the
   wide layouts, by 4× to 220×.
0b. **The PIT dimension is nearly free on wide layouts via overlay** —
   as-of queries (CS5/TS6) cost A/B only ~0.2s over their non-PIT twins by
   joining the normalized version-2 rows onto the v1 wide base, while C
   pays `arg_max` over every row it touches (26s / 23s warm). The wide
   layouts' one structural gap closes with a cheap hybrid pattern.
1. **The generic-slot design costs nothing measurable at query time: B ≈ A
   within 4–6% on every query,** cold and warm, at full global scale. The
   265-column physical schema (NULL-padded slots, slot-aliasing views) is
   free — dictionary/RLE absorbs the NULLs, projection pushdown reads only
   aliased columns. Combined with B's structural add-a-model advantage
   (constant object count, mapping rows instead of DDL), **B is the
   recommended physical strategy** pending the DRILL measurement.
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
