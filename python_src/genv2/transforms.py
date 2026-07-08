"""V2 transforms (spec §3), S3-native and global-scale aware.

Strategy A — per-model wide tables (wide_cs date-major monthly partitions,
wide_ts asset-major buckets). Strategy B — one generic-slot table
(F001..F260, model_id-first partitions) + factor_slot_map + generated views.

Scale/memory design (sized for r7gd.4xlarge, 16 vCPU / 128 GB / NVMe temp):
- pivots run per QUARTER for global-scale models (hash-agg state ~15 GB)
  and per year for regionals;
- ts buckets are built per YEAR from an in-memory temp of that year's wide
  rows (~30 GB for a global model), writing bucket=<b>/data_<year>.parquet —
  multi-file buckets, each file sorted (asset_id, cob_date);
- every destination may be local or s3:// (direct writes, no staging).
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import duckdb

from .fleet import REGIONS, V2Config, V2Model
from .writer import is_s3

MAX_SLOTS = 260
BUCKETS = 32
COPY_OPTS = "FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 3"
BIG_MODEL_YEAR_ROWS = 60_000_000


def _connect(*roots) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(f"SET threads = {max(4, (os.cpu_count() or 8) - 1)}")
    con.execute("SET memory_limit = '90GB'") if (os.cpu_count() or 0) >= 16 else None
    tmp = os.environ.get("DUCK_TMP")
    if tmp:
        con.execute(f"SET temp_directory = '{tmp}'")
    if any(is_s3(r) for r in roots):
        con.execute("INSTALL httpfs; LOAD httpfs;")
        con.execute(f"""CREATE SECRET (TYPE s3,
            KEY_ID '{os.environ["AWS_ACCESS_KEY_ID"]}',
            SECRET '{os.environ["AWS_SECRET_ACCESS_KEY"]}',
            REGION 'eu-west-1')""")
    return con


def _fresh_dir(root: str) -> None:
    if not is_s3(root):
        shutil.rmtree(root, ignore_errors=True)
        Path(root).mkdir(parents=True)


def _is_big(m: V2Model) -> bool:
    cov = sum(REGIONS[r] for r in m.universe_regions) * m.coverage_rate
    nnz = m.n_styles + 2.1 + (2 if m.n_countries else 0)
    return cov * nnz * 261 > BIG_MODEL_YEAR_ROWS


def _periods(m: V2Model, years: list[int]) -> list[tuple[str, str]]:
    """(from, to) date ranges: quarterly for big models, yearly otherwise."""
    if not _is_big(m):
        return [(f"{y}-01-01", f"{y}-12-31") for y in years]
    qs = [("01-01", "03-31"), ("04-01", "06-30"), ("07-01", "09-30"), ("10-01", "12-31")]
    return [(f"{y}-{a}", f"{y}-{b}") for y in years for a, b in qs]


def _pivot_sql(m: V2Model, normalized: str, lo: str, hi: str) -> str:
    aggs = []
    for fid, ftype in zip(m.factor_ids, m.factor_types):
        a = f"max(l.value) FILTER (WHERE l.factor_id = '{fid}')"
        if ftype in ("INDUSTRY", "COUNTRY", "CURRENCY"):
            a = f"coalesce({a}, 0.0)"
        aggs.append(f'{a} AS "{fid}"')
    rng = f"cob_date BETWEEN DATE '{lo}' AND DATE '{hi}'"
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


def _bucketize(con, src_glob: str, target: str, years: list[int],
               select: str = "*") -> None:
    """Per-year temp table -> per-bucket sorted files (bounded memory)."""
    for y in years:
        con.execute(f"""
            CREATE OR REPLACE TEMP TABLE yt AS
            SELECT {select} FROM read_parquet('{src_glob}', hive_partitioning=true)
            WHERE year_month LIKE '{y}-%'
        """)
        if con.execute("SELECT count(*) FROM yt").fetchone()[0] == 0:
            continue
        for b in range(BUCKETS):
            dest = f"{target}/bucket={b}/data_{y}.parquet"
            if not is_s3(dest):
                Path(dest).parent.mkdir(parents=True, exist_ok=True)
            con.execute(f"""
                COPY (SELECT * FROM yt WHERE asset_id % {BUCKETS} = {b}
                      ORDER BY asset_id, cob_date)
                TO '{dest}' ({COPY_OPTS})
            """)
        con.execute("DROP TABLE yt")


def build_strategy_a(cfg: V2Config, normalized: str, out: str,
                     years: list[int], quiet=False) -> None:
    con = _connect(normalized, out)
    for m in cfg.models:
        cs = f"{out}/wide_cs/model_id={m.model_id}"
        _fresh_dir(cs)
        t0 = time.perf_counter()
        for lo, hi in _periods(m, years):
            con.execute(f"""
                COPY (SELECT strftime(cob_date, '%Y-%m') AS year_month, *
                      FROM ({_pivot_sql(m, normalized, lo, hi)})
                      ORDER BY cob_date, asset_id)
                TO '{cs}' ({COPY_OPTS}, PARTITION_BY (year_month), APPEND)
            """)
        cs_s = time.perf_counter() - t0

        ts = f"{out}/wide_ts/model_id={m.model_id}"
        _fresh_dir(ts)
        t0 = time.perf_counter()
        _bucketize(con, f"{cs}/**/*.parquet", ts, years,
                   select="* EXCLUDE (year_month)")
        if not quiet:
            print(f"A {m.model_id}: cs {cs_s:.0f}s, ts {time.perf_counter() - t0:.0f}s",
                  flush=True)


def _slot_select(m: V2Model) -> str:
    by_slot = {seq + 1: fid for seq, fid in enumerate(m.factor_ids)}
    return ", ".join(f'"{by_slot[s]}" AS F{s:03d}' if s in by_slot
                     else f"CAST(NULL AS DOUBLE) AS F{s:03d}"
                     for s in range(1, MAX_SLOTS + 1))


def build_strategy_b(cfg: V2Config, out_a: str, out: str,
                     years: list[int], quiet=False) -> None:
    import pyarrow as pa
    from .writer import write_any

    con = _connect(out_a, out)
    for m in cfg.models:
        src = f"{out_a}/wide_cs/model_id={m.model_id}"
        gcs = f"{out}/generic_cs/model_id={m.model_id}"
        _fresh_dir(gcs)
        t0 = time.perf_counter()
        for y in years:
            con.execute(f"""
                COPY (SELECT year_month, cob_date, asset_id, specific_risk, {_slot_select(m)}
                      FROM read_parquet('{src}/**/*.parquet', hive_partitioning=true)
                      WHERE year_month LIKE '{y}-%'
                      ORDER BY cob_date, asset_id)
                TO '{gcs}' ({COPY_OPTS}, PARTITION_BY (year_month), APPEND)
            """)
        gcs_s = time.perf_counter() - t0

        gts = f"{out}/generic_ts/model_id={m.model_id}"
        _fresh_dir(gts)
        t0 = time.perf_counter()
        _bucketize(con, f"{gcs}/**/*.parquet", gts, years,
                   select="* EXCLUDE (year_month)")
        if not quiet:
            print(f"B {m.model_id}: cs {gcs_s:.0f}s, ts {time.perf_counter() - t0:.0f}s",
                  flush=True)

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
    write_any(pa.table(rows), f"{out}/factor_slot_map.parquet", cfg)
    views_sql = "\n\n".join(views) + "\n"
    if is_s3(out):
        import boto3
        b, _, key = out.removeprefix("s3://").partition("/")
        boto3.client("s3", region_name="eu-west-1").put_object(
            Bucket=b, Key=f"{key}/views.sql", Body=views_sql.encode())
    else:
        Path(f"{out}/views.sql").write_text(views_sql)
    if not quiet:
        print(f"slot map + {len(views)} views -> {out}")


def build(cfg: V2Config, normalized: str, out_root: str, strategy: str,
          quiet=False) -> None:
    years = list(range(cfg.start_date.year, cfg.end_date.year + 1))
    normalized = str(normalized).rstrip("/")
    out_root = str(out_root).rstrip("/")
    if strategy in ("a", "both"):
        build_strategy_a(cfg, normalized, f"{out_root}/transforms_a", years, quiet)
    if strategy in ("b", "both"):
        build_strategy_b(cfg, f"{out_root}/transforms_a", f"{out_root}/transforms_b",
                         years, quiet)
