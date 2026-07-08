"""V2 transforms (spec §3).

Strategy A — per-model wide tables (v1 pattern): wide_cs (date-major, monthly
partitions) and wide_ts (asset-major buckets), one pair per model, schemas
generated from the factor taxonomy.

Strategy B — one generic-slot table: identical rows for ALL models under a
single 260-slot schema (F001..F260, slot = factor_seq + 1, NULL where a model
has no factor), partitioned model_id-first; plus factor_slot_map.parquet and
generated per-model views (views.sql) — the only intended query surface.

Both build from the same v2 normalized store, version_id = 1 rows only
(restated v2 rows are the PIT dimension, exercised via as-of queries, not
baked into the transforms).
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import duckdb

from transforms.build import write_buckets   # v1 helper, unchanged

from .fleet import V2Config, V2Model

MAX_SLOTS = 260
BUCKETS = 32
COPY_OPTS = "FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 3"


def _pivot_sql(m: V2Model, normalized: Path, year: int) -> str:
    aggs = []
    for seq, (fid, ftype) in enumerate(zip(m.factor_ids, m.factor_types)):
        a = f"max(l.value) FILTER (WHERE l.factor_id = '{fid}')"
        if ftype in ("INDUSTRY", "COUNTRY", "CURRENCY"):
            a = f"coalesce({a}, 0.0)"
        aggs.append(f'{a} AS "{fid}"')
    rng = f"cob_date BETWEEN DATE '{year}-01-01' AND DATE '{year}-12-31'"
    return f"""
        WITH wide AS (
            SELECT l.cob_date, l.asset_id, {", ".join(aggs)}
            FROM read_parquet('{normalized}/factor_loading/model_id={m.model_id}/**/*.parquet') l
            WHERE {rng} AND l.version_id = 1
            GROUP BY 1, 2
        )
        SELECT w.*, s.value AS specific_risk
        FROM wide w
        JOIN (SELECT cob_date, asset_id, value
              FROM read_parquet('{normalized}/specific_risk/model_id={m.model_id}/**/*.parquet')
              WHERE {rng} AND version_id = 1) s
        USING (cob_date, asset_id)
    """


def build_strategy_a(cfg: V2Config, normalized: Path, out: Path,
                     years: list[int], quiet=False) -> list[dict]:
    con = duckdb.connect()
    results = []
    for m in cfg.models:
        cs = out / "wide_cs" / f"model_id={m.model_id}"
        shutil.rmtree(cs, ignore_errors=True)
        cs.mkdir(parents=True)
        t0 = time.perf_counter()
        for y in years:
            con.execute(f"""
                COPY (SELECT strftime(cob_date, '%Y-%m') AS year_month, *
                      FROM ({_pivot_sql(m, normalized, y)})
                      ORDER BY cob_date, asset_id)
                TO '{cs}' ({COPY_OPTS}, PARTITION_BY (year_month), APPEND)
            """)
        cs_s = time.perf_counter() - t0

        ts = out / "wide_ts" / f"model_id={m.model_id}"
        shutil.rmtree(ts, ignore_errors=True)
        ts.mkdir(parents=True)
        t0 = time.perf_counter()
        con.execute(f"CREATE OR REPLACE TEMP TABLE wtmp AS "
                    f"SELECT * EXCLUDE (year_month) FROM "
                    f"read_parquet('{cs}/**/*.parquet', hive_partitioning=true)")
        write_buckets(con, "wtmp", ts, BUCKETS)
        con.execute("DROP TABLE wtmp")
        r = {"model_id": m.model_id, "cs_s": round(cs_s, 1),
             "ts_s": round(time.perf_counter() - t0, 1)}
        results.append(r)
        if not quiet:
            print(f"A {m.model_id}: cs {r['cs_s']}s, ts {r['ts_s']}s", flush=True)
    return results


def _slot_select(m: V2Model) -> str:
    """Named factor columns -> F001..F260 (NULL-padded), from a wide_cs scan."""
    by_slot = {seq + 1: fid for seq, fid in enumerate(m.factor_ids)}
    cols = [f'"{by_slot[s]}" AS F{s:03d}' if s in by_slot
            else f"CAST(NULL AS DOUBLE) AS F{s:03d}"
            for s in range(1, MAX_SLOTS + 1)]
    return ", ".join(cols)


def build_strategy_b(cfg: V2Config, out_a: Path, out: Path,
                     years: list[int], quiet=False) -> None:
    """Generic-slot table built from strategy A output (already pivoted)."""
    import pyarrow as pa
    from generator.writer import write_parquet

    con = duckdb.connect()
    for m in cfg.models:
        src = out_a / "wide_cs" / f"model_id={m.model_id}"
        gcs = out / "generic_cs" / f"model_id={m.model_id}"
        shutil.rmtree(gcs, ignore_errors=True)
        gcs.mkdir(parents=True)
        t0 = time.perf_counter()
        con.execute(f"""
            COPY (SELECT year_month, cob_date, asset_id, specific_risk, {_slot_select(m)}
                  FROM read_parquet('{src}/**/*.parquet', hive_partitioning=true)
                  ORDER BY cob_date, asset_id)
            TO '{gcs}' ({COPY_OPTS}, PARTITION_BY (year_month), OVERWRITE)
        """)
        gcs_s = time.perf_counter() - t0

        gts = out / "generic_ts" / f"model_id={m.model_id}"
        shutil.rmtree(gts, ignore_errors=True)
        gts.mkdir(parents=True)
        t0 = time.perf_counter()
        con.execute(f"CREATE OR REPLACE TEMP TABLE gtmp AS "
                    f"SELECT * EXCLUDE (year_month) FROM "
                    f"read_parquet('{gcs}/**/*.parquet', hive_partitioning=true)")
        write_buckets(con, "gtmp", gts, BUCKETS)
        con.execute("DROP TABLE gtmp")
        if not quiet:
            print(f"B {m.model_id}: cs {gcs_s:.1f}s, ts {time.perf_counter() - t0:.1f}s",
                  flush=True)

    # slot map (temporal, append-only) + generated views: the query surface
    rows = {"model_id": [], "slot_id": [], "factor_id": [], "factor_seq": [],
            "valid_from": [], "valid_to": []}
    views = []
    for m in cfg.models:
        named = ", ".join(f'F{seq + 1:03d} AS "{fid}"'
                          for seq, fid in enumerate(m.factor_ids))
        for layout in ("generic_cs", "generic_ts"):
            views.append(
                f"CREATE OR REPLACE VIEW v_{layout[-2:]}_{m.model_id.lower()} AS\n"
                f"  SELECT cob_date, asset_id, {named}, specific_risk\n"
                f"  FROM read_parquet('{out}/{layout}/model_id={m.model_id}/**/*.parquet', "
                f"hive_partitioning=true);")
        for seq, fid in enumerate(m.factor_ids):
            rows["model_id"].append(m.model_id)
            rows["slot_id"].append(seq + 1)
            rows["factor_id"].append(fid)
            rows["factor_seq"].append(seq)
            rows["valid_from"].append(cfg.start_date)
            rows["valid_to"].append(None)
    write_parquet(pa.table(rows), out / "factor_slot_map.parquet", cfg)
    (out / "views.sql").write_text("\n\n".join(views) + "\n")
    if not quiet:
        print(f"slot map + {len(views)} views -> {out}")


def build(cfg: V2Config, normalized: Path, out_root: Path, strategy: str,
          quiet=False) -> None:
    years = list(range(cfg.start_date.year, cfg.end_date.year + 1))
    if strategy in ("a", "both"):
        build_strategy_a(cfg, normalized, out_root / "transforms_a", years, quiet)
    if strategy in ("b", "both"):
        build_strategy_b(cfg, out_root / "transforms_a", out_root / "transforms_b",
                         years, quiet)
