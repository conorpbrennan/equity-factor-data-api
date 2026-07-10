"""V2 three-way storage benchmark (generator-spec-v2.md §3–§4).

Arms, all DuckDB, identical logical queries, AX_WW4_MH (248 factors, 58k
coverage) unless stated:

  A — per-model wide tables (transforms_a wide_cs/wide_ts Parquet)
  B — generic-slot table queried through the generated per-model views
      (transforms_b generic_cs/generic_ts; 265-column physical schema)
  C — normalized long store, pivoted at query time

Suite: CS1/TS1 (continuity with v1) + CHAIN1/CHAIN2 (serial research
sessions, timed connect→last DataFrame) per arm; FMP1/FMP2 once (the fmp
store is shared by every arm). Cold = fresh process + fadvise eviction of
the arm's data dirs; warm = in-process repeats. No-spill budgets.
"""

ARMS = ("A_permodel", "B_generic", "C_normalized")
QUERIES = ("CS1", "CS2", "CS3", "CS4", "CS5", "CS6",
           "TS1", "TS2", "TS3", "TS4", "TS5", "TS6")
CHAIN_QUERIES = ("CHAIN1", "CHAIN2")   # measured separately (see results)
SHARED_QUERIES = ()                     # FMP1/2 measured separately
