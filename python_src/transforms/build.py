"""Transform construction. Wall-time and output size are recorded per step —
the plan treats these as the future daily-pipeline cost, so they are outputs,
not incidental logging."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import duckdb

from generator.config import GeneratorConfig, ModelConfig

from . import DEFAULT_BUCKETS

COPY_OPTS = "FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 3"


def _dir_stats(path: Path) -> tuple[int, int]:
    files = list(path.rglob("*.parquet"))
    return len(files), sum(f.stat().st_size for f in files)


def _rows(con: duckdb.DuckDBPyConnection, path: Path) -> int:
    return con.execute(
        f"SELECT count(*) FROM read_parquet('{path}/**/*.parquet')").fetchone()[0]


def pivot_sql(m: ModelConfig, normalized: Path, where: str | None = None) -> str:
    """Explicit conditional aggregation (plan: faster and more predictable
    than PIVOT). Absent industry loadings densify to 0.0; styles and market
    are always present in the sparse store.

    `where` must be a predicate on cob_date only (e.g. a date range) so it
    pushes down to Parquet footer stats and prunes whole year files."""
    cols = []
    for fid, ftype in zip(m.factor_ids, m.factor_types):
        agg = f"max(l.value) FILTER (WHERE l.factor_id = '{fid}')"
        if ftype == "INDUSTRY":
            agg = f"coalesce({agg}, 0.0)"
        cols.append(f'{agg} AS "{fid}"')
    date_filter = f"WHERE {where}" if where else ""
    return f"""
        WITH wide AS (
            SELECT l.cob_date, l.asset_id,
                   {", ".join(cols)}
            FROM read_parquet('{normalized}/factor_loading/model_id={m.model_id}/**/*.parquet') l
            {date_filter}
            GROUP BY l.cob_date, l.asset_id
        )
        SELECT w.*, s.value AS specific_risk
        FROM wide w
        JOIN (
            SELECT cob_date, asset_id, value
            FROM read_parquet('{normalized}/specific_risk/model_id={m.model_id}/**/*.parquet')
            {date_filter}
        ) s USING (cob_date, asset_id)
    """


def build_cs(con: duckdb.DuckDBPyConnection, m: ModelConfig, normalized: Path,
             out_root: Path, years: list[int]) -> dict:
    """Transform A. Pivoted per year (bounded memory, and each year-pass is
    exactly the shape of a backfill pipeline run) appended into monthly
    partitions — each month receives exactly one file."""
    target = out_root / "wide_cs" / f"model_id={m.model_id}"
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    t0 = time.perf_counter()
    for y in years:
        year_range = (f"cob_date BETWEEN DATE '{y}-01-01' AND DATE '{y}-12-31'")
        con.execute(f"""
            COPY (
                SELECT strftime(cob_date, '%Y-%m') AS year_month, *
                FROM ({pivot_sql(m, normalized, where=year_range)})
                ORDER BY cob_date, asset_id
            ) TO '{target}' ({COPY_OPTS}, PARTITION_BY (year_month), APPEND)
        """)
    elapsed = time.perf_counter() - t0
    n_files, n_bytes = _dir_stats(target)
    return {"transform": "wide_cs", "model_id": m.model_id,
            "rows": _rows(con, target), "files": n_files, "bytes": n_bytes,
            "seconds": round(elapsed, 1)}


def write_buckets(con: duckdb.DuckDBPyConnection, source: str, target: Path,
                  buckets: int) -> None:
    """One order-preserving COPY per bucket. DuckDB's parallel PARTITION_BY
    writer does not guarantee sort order within partition files, and B's whole
    point is a contiguous (asset_id, cob_date) run per bucket."""
    for b in range(buckets):
        bdir = target / f"bucket={b}"
        bdir.mkdir(parents=True, exist_ok=True)
        con.execute(f"""
            COPY (
                SELECT * FROM {source}
                WHERE asset_id % {buckets} = {b}
                ORDER BY asset_id, cob_date
            ) TO '{bdir / "data_0.parquet"}' ({COPY_OPTS})
        """)


def build_ts(con: duckdb.DuckDBPyConnection, m: ModelConfig, out_root: Path,
             buckets: int) -> dict:
    """Transform B: re-sort/re-partition pass over Transform A's output.
    A is materialized once into a temp table so the 32 bucket writes don't
    each re-decompress the Parquet."""
    src = out_root / "wide_cs" / f"model_id={m.model_id}"
    target = out_root / "wide_ts" / f"model_id={m.model_id}"
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    t0 = time.perf_counter()
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE wide_tmp AS
        SELECT * EXCLUDE (year_month)
        FROM read_parquet('{src}/**/*.parquet', hive_partitioning=true)
    """)
    write_buckets(con, "wide_tmp", target, buckets)
    con.execute("DROP TABLE wide_tmp")
    elapsed = time.perf_counter() - t0
    n_files, n_bytes = _dir_stats(target)
    return {"transform": "wide_ts", "model_id": m.model_id,
            "rows": _rows(con, target), "files": n_files, "bytes": n_bytes,
            "seconds": round(elapsed, 1)}


def build(cfg: GeneratorConfig, normalized: Path, out_root: Path,
          buckets: int = DEFAULT_BUCKETS) -> list[dict]:
    con = duckdb.connect()
    years = list(range(cfg.start_date.year, cfg.end_date.year + 1))
    results = []
    for m in cfg.models:
        for step in (lambda: build_cs(con, m, normalized, out_root, years),
                     lambda: build_ts(con, m, out_root, buckets)):
            r = step()
            print(f"{r['transform']}/{r['model_id']}: {r['rows']:,} rows, "
                  f"{r['files']} files, {r['bytes'] / 2**30:.2f} GiB "
                  f"[{r['seconds']}s]")
            results.append(r)
    report = {"buckets": buckets, "normalized": str(normalized),
              "results": results}
    report_path = out_root / "report.json"
    report_path.write_text(json.dumps(report, indent=2))
    print(f"report -> {report_path}")
    return results


def incremental_probe(cfg: GeneratorConfig, normalized: Path, out_root: Path,
                      cob_date: str, buckets: int = DEFAULT_BUCKETS) -> list[dict]:
    """Measure the daily-incremental cost (plan: B's append is the awkward one).

    Writes one date's output to an isolated probe directory rather than
    appending to the benchmark data (which would duplicate the date). The
    per-file/per-byte shape is identical to what a real daily append would add.
    """
    probe = out_root / "_incremental_probe" / cob_date
    shutil.rmtree(probe, ignore_errors=True)
    con = duckdb.connect()
    results = []
    for m in cfg.models:
        cs_target = probe / "wide_cs" / f"model_id={m.model_id}"
        cs_target.mkdir(parents=True)
        t0 = time.perf_counter()
        con.execute(f"""
            COPY (
                SELECT strftime(cob_date, '%Y-%m') AS year_month, *
                FROM ({pivot_sql(m, normalized, where=f"cob_date = DATE '{cob_date}'")})
                ORDER BY cob_date, asset_id
            ) TO '{cs_target}' ({COPY_OPTS}, PARTITION_BY (year_month), APPEND)
        """)
        cs_s = time.perf_counter() - t0

        ts_target = probe / "wide_ts" / f"model_id={m.model_id}"
        ts_target.mkdir(parents=True)
        t0 = time.perf_counter()
        write_buckets(
            con,
            f"(SELECT * EXCLUDE (year_month) FROM "
            f"read_parquet('{cs_target}/**/*.parquet', hive_partitioning=true))",
            ts_target, buckets)
        ts_s = time.perf_counter() - t0

        for name, target, secs in (("wide_cs", cs_target, cs_s),
                                   ("wide_ts", ts_target, ts_s)):
            n_files, n_bytes = _dir_stats(target)
            r = {"transform": name, "model_id": m.model_id, "date": cob_date,
                 "files": n_files, "bytes": n_bytes, "seconds": round(secs, 2)}
            print(f"incremental {name}/{m.model_id} {cob_date}: "
                  f"{n_files} files, {n_bytes / 2**10:.0f} KiB [{r['seconds']}s]")
            results.append(r)
    return results
