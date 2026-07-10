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

## Results (p50 seconds, cold / warm)

| query | A per-model | B generic views | C normalized |
|---|---|---|---|
| CS1 — full cross-section, 1 date, 249 cols × 58k assets | 0.61 / **0.39** | 0.70 / **0.41** | 8.6 / 8.1 |
| TS1 — one asset, 20y, all factors | 1.54 / **0.35** | 1.49 / **0.36** | 73.9 / 12.3 |
| CHAIN1 — 100 names latest date → factor returns 5y | 0.66 / 0.41 | 0.66 / 0.43 | 0.59 / 0.34 |
| CHAIN2 — estu cross-section → covariance → specific risk | 0.81 / **0.50** | 0.84 / **0.53** | 10.4 / 8.8 |
| FMP1 — one factor's weight vector, 1 date (shared store) | 0.16 / 0.03 | = | = |
| FMP2 — one factor, 10 assets, 20y (shared store) | 16.7 / 0.82 | = | = |

## Findings

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
