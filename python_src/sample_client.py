"""Sample research client: query the factor store on S3 via DuckLake.

The remote-research pattern from the benchmark, end to end:

  1. download the DuckLake catalog file from S3 (4.5 MB — the entire
     metadata layer: tables, snapshots, data-file list, per-file column stats);
  2. attach it read-only in DuckDB;
  3. run the six benchmark queries against the right layout for each —
     for BOTH risk models (Barra- and Axioma-style, each with its own wide
     schema and factor taxonomy) — printing first-run and repeat timings.

Everything data-sized stays in S3; DuckDB range-reads only the bytes each
query needs, planned from the local catalog with zero S3 metadata round trips.

No AWS account needed: the demo prefixes are public-read, and this client
falls back to anonymous (unsigned) requests when no credentials are present.

    .venv/bin/python python_src/sample_client.py [--cache]

--cache loads the community cache_httpfs extension, which persists fetched
S3 ranges to local disk — so the *next* process starts warm too (first-touch
drops from seconds to ~30 ms once any earlier run has fetched the ranges).
Without it, warmth lives only in DuckDB's in-memory cache and dies with the
process.

Requires: duckdb, polars, boto3.
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

import boto3
import duckdb
import polars as pl
from botocore import UNSIGNED
from botocore.config import Config

ANONYMOUS = not os.environ.get("AWS_ACCESS_KEY_ID")

BUCKET = "equity-factor-data-651406457779"
CATALOG_KEY = "results/ec2_s3/catalog.ducklake"
REGION = "eu-west-1"
CATALOG_LOCAL = Path.home() / ".cache" / "factor-store" / "catalog.ducklake"

# Frozen benchmark parameters (see data/benchmark/manifest.json). Factor
# identity is model-scoped — "MOMENTUM" and "MT_MOMENTUM" are different
# factors — and so is coverage, hence per-model TS1 assets.
CS_DATE = "2016-06-15"
TS2_RANGE = ("2018-01-01", "2022-12-31")
TS2_ASSETS = (4, 9, 12, 15, 16, 17, 18, 19, 20, 21, 27, 28, 29, 30, 31, 35, 41,
              44, 47, 54, 58, 60, 61, 66, 71, 72, 75, 76, 81, 83, 85, 88, 89,
              96, 97, 98, 102, 106, 110, 113, 114, 115, 116, 121, 123, 124,
              126, 131, 133, 135, 137, 141, 148, 149, 152, 160, 165, 168, 170,
              175, 176, 178, 179, 183, 185, 192, 194, 195, 199, 200, 201, 209,
              212, 213, 215, 219, 222, 223, 227, 232, 239, 240, 241, 247, 248,
              250, 253, 258, 260, 262, 263, 264, 266, 270, 271, 277, 278, 280,
              282, 288)

MODELS = {
    "BARRA_USE4_L": {
        "suffix": "",              # table names: cs_wide, ts_wide, ...
        "five": ("BETA", "MOMENTUM", "SIZE", "EARNYLD", "RESVOL"),
        "ts_asset": 21,
        "ts2_factors": ("MOMENTUM", "EARNYLD", "GROWTH"),
        "cov_pair": ("BETA", "MOMENTUM"),
    },
    "AXIOMA_US4_MH": {
        "suffix": "_axioma",       # cs_wide_axioma, ts_wide_axioma, ...
        "five": ("MARKET_SENSITIVITY", "MT_MOMENTUM", "ST_MOMENTUM", "SIZE", "VALUE"),
        "ts_asset": 12,            # asset 21 is not in Axioma's coverage universe
        "ts2_factors": ("MT_MOMENTUM", "SIZE", "GROWTH"),
        "cov_pair": ("MARKET_SENSITIVITY", "MT_MOMENTUM"),
    },
}


def make_queries(m: dict) -> dict[str, tuple[str, list[str]]]:
    """The six benchmark queries against one model's tables. Each hits the
    layout built for it: cs_wide* is date-major, ts_wide* asset-major;
    CS3 is two result sets — everything for B·F·Bᵀ + D on one date."""
    sx = m["suffix"]
    five = ", ".join(f'"{f}"' for f in m["five"])
    ts2f = ", ".join(f'"{f}"' for f in m["ts2_factors"])
    ids = ", ".join(map(str, TS2_ASSETS))
    f1, f2 = m["cov_pair"]
    return {
        "CS1": ("full cross-section, one date, all factors", [
            f"SELECT * FROM dl.cs_wide{sx} WHERE cob_date = DATE '{CS_DATE}' ORDER BY asset_id"]),
        "CS2": ("one date, 5 factors, estimation universe only", [f"""
            SELECT w.asset_id, {five}
            FROM dl.cs_wide{sx} w
            JOIN dl.membership{sx} u
              ON u.cob_date = w.cob_date AND u.asset_id = w.asset_id
             AND u.estimation_universe_flag
            WHERE w.cob_date = DATE '{CS_DATE}' ORDER BY w.asset_id"""]),
        "CS3": ("cross-section + covariance + specific risk (B·F·Bᵀ + D)", [
            f"SELECT * FROM dl.cs_wide{sx} WHERE cob_date = DATE '{CS_DATE}' ORDER BY asset_id",
            f"SELECT factor_id_1, factor_id_2, value FROM dl.covariance{sx} "
            f"WHERE cob_date = DATE '{CS_DATE}'"]),
        "TS1": ("one asset, 20 years, all factors", [
            f"SELECT * FROM dl.ts_wide{sx} WHERE asset_id = {m['ts_asset']} ORDER BY cob_date"]),
        "TS2": ("100 assets, 5 years, 3 factors (mixed)", [f"""
            SELECT cob_date, asset_id, {ts2f}
            FROM dl.ts_wide{sx}
            WHERE asset_id IN ({ids})
              AND cob_date BETWEEN DATE '{TS2_RANGE[0]}' AND DATE '{TS2_RANGE[1]}'
            ORDER BY asset_id, cob_date"""]),
        "TS3": ("one covariance pair, 20 years", [
            f"SELECT cob_date, value FROM dl.covariance{sx} "
            f"WHERE factor_id_1 = '{f1}' AND factor_id_2 = '{f2}' ORDER BY cob_date"]),
    }


def fetch_catalog() -> None:
    CATALOG_LOCAL.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    cfg = Config(signature_version=UNSIGNED) if ANONYMOUS else None
    boto3.client("s3", region_name=REGION, config=cfg).download_file(
        BUCKET, CATALOG_KEY, str(CATALOG_LOCAL))
    size = CATALOG_LOCAL.stat().st_size
    print(f"catalog: s3://{BUCKET}/{CATALOG_KEY} -> {CATALOG_LOCAL}"
          f" ({size / 2**20:.1f} MiB, {time.perf_counter() - t0:.2f}s)")


def connect(persistent_cache: bool = False) -> duckdb.DuckDBPyConnection:
    t0 = time.perf_counter()
    con = duckdb.connect()
    cache_state = "in-memory cache only"
    if persistent_cache:
        try:
            # must load before other filesystems so it can wrap them
            con.execute("INSTALL cache_httpfs FROM community; LOAD cache_httpfs;")
            con.execute(f"SET cache_httpfs_cache_directory = "
                        f"'{CATALOG_LOCAL.parent / 'httpfs'}'")
            cache_state = "persistent disk cache (cache_httpfs)"
        except Exception as e:
            print(f"cache_httpfs unavailable, continuing without: {e}")
    con.execute("INSTALL httpfs; INSTALL ducklake;")
    if ANONYMOUS:
        # empty-config secret: pins the region, requests go unsigned
        con.execute(f"CREATE SECRET s3cred (TYPE s3, PROVIDER config, REGION '{REGION}')")
    else:
        con.execute(f"""
            CREATE SECRET s3cred (TYPE s3,
                KEY_ID '{os.environ["AWS_ACCESS_KEY_ID"]}',
                SECRET '{os.environ["AWS_SECRET_ACCESS_KEY"]}',
                REGION '{REGION}')
        """)
    con.execute(f"ATTACH 'ducklake:{CATALOG_LOCAL}' AS dl (READ_ONLY)")
    print(f"attach: {time.perf_counter() - t0:.2f}s "
          f"({'anonymous' if ANONYMOUS else 'authenticated'}; {cache_state})\n")
    return con


def run_query(con: duckdb.DuckDBPyConnection, sqls: list[str]) -> tuple[float, int]:
    t0 = time.perf_counter()
    rows = 0
    for sql in sqls:
        df = pl.from_arrow(con.execute(sql).to_arrow_table())
        rows += df.height
    return time.perf_counter() - t0, rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache", action="store_true",
                    help="persist fetched S3 ranges to disk (cache_httpfs) so "
                         "future processes start warm")
    args = ap.parse_args()
    fetch_catalog()
    con = connect(persistent_cache=args.cache)
    for model_id, m in MODELS.items():
        n_cols = len(con.execute(
            f"SELECT * FROM dl.cs_wide{m['suffix']} LIMIT 0").description)
        print(f"-- {model_id} ({n_cols} wide columns)")
        print(f"{'query':<6} {'first':>9} {'repeat':>9} {'rows':>9}  description")
        for qid, (desc, sqls) in make_queries(m).items():
            first_s, rows = run_query(con, sqls)
            repeat_s, _ = run_query(con, sqls)
            print(f"{qid:<6} {first_s * 1000:>7.0f}ms {repeat_s * 1000:>7.0f}ms "
                  f"{rows:>9,}  {desc}")
        print()


if __name__ == "__main__":
    main()
