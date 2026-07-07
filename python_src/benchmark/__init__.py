"""Stage 3 benchmark harness (factor-model-benchmark-plan.md §4 Stage 3).

Arms (all DuckDB, identical logical queries):
- normalized   — pivot-at-query-time over the normalized Parquet (the yardstick)
- wide_cs      — Transform A Parquet (date-major, monthly hive partitions)
- wide_ts      — Transform B Parquet (asset-major, bucket hive partitions)
- duckdb_cs/ts — DuckDB native tables loaded from A/B in matching sort order
- ducklake_cs/ts — DuckLake tables over equivalent data (catalog-planned scans)

Queries CS1-CS3 / TS1-TS3 run against EVERY arm; the plan's X1 ("each
transform answering the other view's query") is the off-diagonal of that grid
(TS* on *_cs arms, CS* on *_ts arms), not a separate query.

Metric: time to usable DataFrame — execute + Arrow fetch + Polars conversion,
measured in-process; cold runs use a fresh process with page cache evicted
(posix_fadvise DONTNEED) and include connection/ATTACH cost. Bytes scanned is
the /proc/self/io rchar delta (logical bytes read, engine-agnostic).
"""

QUERIES = ("CS1", "CS2", "CS3", "TS1", "TS2", "TS3")
ARMS = ("normalized", "wide_cs", "wide_ts",
        "duckdb_cs", "duckdb_ts", "ducklake_cs", "ducklake_ts")
