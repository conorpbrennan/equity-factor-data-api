"""Stage 2 transforms (factor-model-benchmark-plan.md §4, generator-spec.md companion).

Builds the two query-optimized wide layouts from the normalized Parquet store,
in pure DuckDB SQL:

- Transform A (wide_cs): one row per (cob_date, asset_id) with named factor
  columns + specific_risk; hive layout model_id=<M>/year_month=<YYYY-MM>/,
  sorted (cob_date, asset_id) within partition.
- Transform B (wide_ts): same row shape re-sorted/re-partitioned from A;
  layout model_id=<M>/bucket=<asset_id % N>/, sorted (asset_id, cob_date).

Factor covariance is deliberately NOT re-materialized: both transforms share
the normalized date-sorted table (plan: 90x90 is too small to need two layouts).

Bucketing uses asset_id % N rather than a hash function so the assignment is
stable across DuckDB versions; ids are dense integers, so modulo balances.
"""

DEFAULT_BUCKETS = 32
