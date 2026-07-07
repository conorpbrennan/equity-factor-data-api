"""Benchmark setup: resolve frozen query parameters, build the native-DuckDB
and DuckLake stores, and write the manifest every worker reads."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import duckdb
import numpy as np

from generator.config import GeneratorConfig
from generator.trading_calendar import trading_days
from transforms import DEFAULT_BUCKETS


def _dir_bytes(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _month_list(start: str, end: str) -> list[str]:
    months, (y, m) = [], (int(start[:4]), int(start[5:7]))
    while (y, m) <= (int(end[:4]), int(end[5:7])):
        months.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return months


def resolve_params(cfg: GeneratorConfig, model_id: str, normalized: Path,
                   buckets: int) -> dict:
    m = next(mm for mm in cfg.models if mm.model_id == model_id)
    days = trading_days(cfg.start_date, cfg.end_date)
    first, last = str(days[0]), str(days[-1])
    cs_date = str(days[np.argmin(np.abs(
        (days - np.datetime64("2016-06-15")).astype(int)))])

    # 100 assets with full-calendar history that the model covers (coverage is
    # static, so presence in specific_risk on the last date suffices).
    con = duckdb.connect()
    ids = [r[0] for r in con.execute(f"""
        SELECT a.asset_id
        FROM read_parquet('{normalized}/asset_master.parquet') a
        JOIN read_parquet('{normalized}/specific_risk/model_id={model_id}/**/*.parquet') s
          ON s.asset_id = a.asset_id AND s.cob_date = DATE '{last}'
        WHERE a.start_date = DATE '{first}' AND a.end_date IS NULL
        ORDER BY a.asset_id LIMIT 100
    """).fetchall()]
    if len(ids) < 100:
        raise RuntimeError(f"only {len(ids)} full-history covered assets found")

    styles = list(m.style_factors)
    ts2_start, ts2_end = "2018-01-01", "2022-12-31"
    return {
        "model_id": model_id,
        "buckets": buckets,
        "factor_ids": m.factor_ids,
        "industry_ids": [f for f, t in zip(m.factor_ids, m.factor_types)
                         if t == "INDUSTRY"],
        "cs_date": cs_date,
        "cs_month": cs_date[:7],
        "five_factors": styles[:5],
        "ts_asset": ids[9],
        "ts2_assets": ids,
        "ts2_start": ts2_start,
        "ts2_end": ts2_end,
        "ts2_months": _month_list(ts2_start, ts2_end),
        "ts2_factors": [styles[1], styles[3], styles[5]],
        "cov_pair": [styles[0], styles[1]],
    }


def build_stores(model_id: str, normalized: Path, transforms_dir: Path,
                 out_dir: Path) -> dict:
    cs = transforms_dir / "wide_cs" / f"model_id={model_id}"
    ts = transforms_dir / "wide_ts" / f"model_id={model_id}"
    mem = normalized / "universe_membership" / f"model_id={model_id}"
    cov = normalized / "factor_covariance" / f"model_id={model_id}"

    loads = {
        "cs_wide": f"SELECT * EXCLUDE (year_month) FROM read_parquet('{cs}/**/*.parquet', "
                   f"hive_partitioning=true) ORDER BY cob_date, asset_id",
        "ts_wide": f"SELECT * EXCLUDE (bucket) FROM read_parquet('{ts}/**/*.parquet', "
                   f"hive_partitioning=true) ORDER BY asset_id, cob_date",
        "membership": f"SELECT cob_date, asset_id, estimation_universe_flag "
                      f"FROM read_parquet('{mem}/**/*.parquet') ORDER BY cob_date, asset_id",
        "covariance": f"SELECT cob_date, factor_id_1, factor_id_2, value "
                      f"FROM read_parquet('{cov}/**/*.parquet') ORDER BY cob_date",
    }

    native_db = out_dir / "native.duckdb"
    native_db.unlink(missing_ok=True)
    t0 = time.perf_counter()
    con = duckdb.connect(str(native_db))
    for name, sql in loads.items():
        con.execute(f"CREATE TABLE {name} AS {sql}")
    con.close()
    native_s = time.perf_counter() - t0
    print(f"native.duckdb: {native_db.stat().st_size / 2**30:.2f} GiB [{native_s:.0f}s]")

    dl_meta = out_dir / "catalog.ducklake"
    dl_data = out_dir / "ducklake_data"
    for p in (dl_meta, Path(str(dl_meta) + ".wal")):
        p.unlink(missing_ok=True)
    shutil.rmtree(dl_data, ignore_errors=True)
    dl_available, dl_s, dl_bytes = False, None, None
    try:
        t0 = time.perf_counter()
        con = duckdb.connect()
        con.execute("INSTALL ducklake")
        con.execute(f"ATTACH 'ducklake:{dl_meta}' AS dl (DATA_PATH '{dl_data}')")
        for name, sql in loads.items():
            con.execute(f"CREATE TABLE dl.{name} AS {sql}")
        con.close()
        dl_s = time.perf_counter() - t0
        dl_available = True
        dl_bytes = _dir_bytes(dl_data) + dl_meta.stat().st_size
        print(f"ducklake: {dl_bytes / 2**30:.2f} GiB [{dl_s:.0f}s]")
    except Exception as e:  # extension download can fail offline
        print(f"ducklake arm disabled: {e}")

    return {
        "native_db": str(native_db), "native_bytes": native_db.stat().st_size,
        "native_seconds": round(native_s, 1),
        "ducklake_available": dl_available, "ducklake_meta": str(dl_meta),
        "ducklake_bytes": dl_bytes,
        "ducklake_seconds": round(dl_s, 1) if dl_s else None,
    }


def setup_s3(bucket: str, local_bench: Path, out_dir: Path,
             region: str = "eu-west-1") -> None:
    """S3 manifest for the remote runs. Query params are copied verbatim from
    the local manifest so both grids answer identical questions. The DuckLake
    catalog stays a local file; its data files are written to S3 (this is the
    upload for the ducklake arms). Native-file arms don't exist remotely."""
    import os

    existing = out_dir / "manifest.json"
    if existing.exists() and not json.loads(existing.read_text()).get("s3"):
        raise SystemExit(f"{existing} is a local manifest — pass a different "
                         f"--output (e.g. data/benchmark_s3)")
    local_man = json.loads((local_bench / "manifest.json").read_text())
    model_id = local_man["model_id"]
    bucket = bucket.rstrip("/")
    out_dir.mkdir(parents=True, exist_ok=True)

    cs = f"{local_man['transforms']}/wide_cs/model_id={model_id}"
    ts = f"{local_man['transforms']}/wide_ts/model_id={model_id}"
    mem = f"{local_man['normalized']}/universe_membership/model_id={model_id}"
    cov = f"{local_man['normalized']}/factor_covariance/model_id={model_id}"
    loads = {
        "cs_wide": f"SELECT * EXCLUDE (year_month) FROM read_parquet('{cs}/**/*.parquet', "
                   f"hive_partitioning=true) ORDER BY cob_date, asset_id",
        "ts_wide": f"SELECT * EXCLUDE (bucket) FROM read_parquet('{ts}/**/*.parquet', "
                   f"hive_partitioning=true) ORDER BY asset_id, cob_date",
        "membership": f"SELECT cob_date, asset_id, estimation_universe_flag "
                      f"FROM read_parquet('{mem}/**/*.parquet') ORDER BY cob_date, asset_id",
        "covariance": f"SELECT cob_date, factor_id_1, factor_id_2, value "
                      f"FROM read_parquet('{cov}/**/*.parquet') ORDER BY cob_date",
    }

    from .arms import _init_s3

    dl_meta = out_dir / "catalog.ducklake"
    for p in (dl_meta, Path(str(dl_meta) + ".wal")):
        p.unlink(missing_ok=True)
    t0 = time.perf_counter()
    con = duckdb.connect()
    con.execute("INSTALL ducklake;")
    _init_s3(con, {"s3": True, "s3_region": region})
    con.execute(f"ATTACH 'ducklake:{dl_meta}' AS dl (DATA_PATH '{bucket}/ducklake')")
    for name, sql in loads.items():
        con.execute(f"CREATE TABLE dl.{name} AS {sql}")
    con.close()
    print(f"ducklake data -> {bucket}/ducklake [{time.perf_counter() - t0:.0f}s]")

    manifest = {
        "model_id": model_id,
        "normalized": f"{bucket}/normalized",
        "transforms": f"{bucket}/transforms",
        "s3": True, "s3_region": region,
        "params": local_man["params"],
        "arms_available": ["normalized", "wide_cs", "wide_ts",
                           "ducklake_cs", "ducklake_ts"],
        "ducklake_available": True, "ducklake_meta": str(dl_meta),
        "native_db": None, "native_bytes": None, "native_seconds": None,
        "ducklake_bytes": None, "ducklake_seconds": None,
    }
    path = out_dir / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2))
    print(f"manifest -> {path}")


def setup(cfg: GeneratorConfig, model_id: str, normalized: Path,
          transforms_dir: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    report = transforms_dir / "report.json"
    buckets = (json.loads(report.read_text())["buckets"]
               if report.exists() else DEFAULT_BUCKETS)
    manifest = {
        "model_id": model_id,
        "normalized": str(normalized),
        "transforms": str(transforms_dir),
        "params": resolve_params(cfg, model_id, normalized, buckets),
    }
    manifest.update(build_stores(model_id, normalized, transforms_dir, out_dir))
    path = out_dir / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2))
    print(f"manifest -> {path}")
