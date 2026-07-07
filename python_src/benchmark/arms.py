"""Per-arm connection factories and SQL builders.

Every arm answers the same six logical queries; a query returns a list of SQL
statements (CS3 needs two result sets: the loadings cross-section and the
covariance matrix). Factor covariance always comes from whichever store the
arm owns — normalized Parquet for the Parquet arms (the shared date-sorted
table), native/DuckLake tables for those arms.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

import duckdb


def _init_s3(con: duckdb.DuckDBPyConnection, man: dict) -> None:
    """For S3 manifests: load httpfs and register credentials — explicit env
    keys where present (laptop / .env), otherwise the AWS credential chain
    (EC2 instance role)."""
    if not man.get("s3"):
        return
    region = man.get("s3_region", "eu-west-1")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    if os.environ.get("AWS_ACCESS_KEY_ID"):
        con.execute(f"""
            CREATE OR REPLACE SECRET s3cred (
                TYPE s3,
                KEY_ID '{os.environ["AWS_ACCESS_KEY_ID"]}',
                SECRET '{os.environ["AWS_SECRET_ACCESS_KEY"]}',
                REGION '{region}'
            )
        """)
    else:
        con.execute("INSTALL aws; LOAD aws;")
        con.execute(f"CREATE OR REPLACE SECRET s3cred "
                    f"(TYPE s3, PROVIDER credential_chain, REGION '{region}')")


def _q(fids: list[str]) -> str:
    return ", ".join(f'"{f}"' for f in fids)


def _strs(vals: list[str]) -> str:
    return ", ".join(f"'{v}'" for v in vals)


def _ints(vals: list[int]) -> str:
    return ", ".join(str(v) for v in vals)


@dataclass(frozen=True)
class Arm:
    name: str
    connect: Callable[[dict], duckdb.DuckDBPyConnection]
    sql: Callable[[str, dict], list[str]]


def _paths(man: dict) -> dict:
    m = man["model_id"]
    return {
        "loading": f"{man['normalized']}/factor_loading/model_id={m}",
        "srisk": f"{man['normalized']}/specific_risk/model_id={m}",
        "mem": f"{man['normalized']}/universe_membership/model_id={m}",
        "cov": f"{man['normalized']}/factor_covariance/model_id={m}",
        "cs": f"{man['transforms']}/wide_cs/model_id={m}",
        "ts": f"{man['transforms']}/wide_ts/model_id={m}",
    }


def _connect_memory(man: dict) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    _init_s3(con, man)
    return con


def _connect_native(man: dict) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(man["native_db"], read_only=True)


def _connect_ducklake(man: dict) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    _init_s3(con, man)
    con.execute(f"ATTACH 'ducklake:{man['ducklake_meta']}' AS dl (READ_ONLY)")
    return con


# ---------------------------------------------------------------- normalized

def _pivot(p: dict, params: dict, where: str) -> str:
    aggs = []
    industries = set(params["industry_ids"])
    for f in params["factor_ids"]:
        a = f"max(l.value) FILTER (WHERE l.factor_id = '{f}')"
        if f in industries:
            a = f"coalesce({a}, 0.0)"
        aggs.append(f'{a} AS "{f}"')
    return f"""
        WITH wide AS (
            SELECT l.cob_date, l.asset_id, {", ".join(aggs)}
            FROM read_parquet('{p["loading"]}/**/*.parquet') l
            WHERE {where} GROUP BY 1, 2
        )
        SELECT w.*, s.value AS specific_risk
        FROM wide w
        JOIN (SELECT cob_date, asset_id, value
              FROM read_parquet('{p["srisk"]}/**/*.parquet') WHERE {where}) s
        USING (cob_date, asset_id)
    """


def _cov_parquet(p: dict, where: str, cols: str) -> str:
    return (f"SELECT {cols} FROM read_parquet('{p['cov']}/**/*.parquet') "
            f"WHERE {where} ORDER BY cob_date")


def _sql_normalized(qid: str, man: dict) -> list[str]:
    p, prm = _paths(man), man["params"]
    d = prm["cs_date"]
    if qid == "CS1":
        where = f"cob_date = DATE '{d}'"
        return [f"SELECT * FROM ({_pivot(p, prm, where)}) ORDER BY asset_id"]
    if qid == "CS2":
        aggs = ", ".join(
            f"max(l.value) FILTER (WHERE l.factor_id = '{f}') AS \"{f}\""
            for f in prm["five_factors"])
        return [f"""
            SELECT l.asset_id, {aggs}
            FROM read_parquet('{p["loading"]}/**/*.parquet') l
            JOIN read_parquet('{p["mem"]}/**/*.parquet') u
              ON u.cob_date = l.cob_date AND u.asset_id = l.asset_id
             AND u.estimation_universe_flag
            WHERE l.cob_date = DATE '{d}'
              AND l.factor_id IN ({_strs(prm["five_factors"])})
            GROUP BY l.asset_id ORDER BY l.asset_id
        """]
    if qid == "CS3":
        return (_sql_normalized("CS1", man)
                + [_cov_parquet(p, f"cob_date = DATE '{d}'",
                                "factor_id_1, factor_id_2, value")])
    if qid == "TS1":
        where = f"asset_id = {prm['ts_asset']}"
        return [f"SELECT * FROM ({_pivot(p, prm, where)}) ORDER BY cob_date"]
    if qid == "TS2":
        aggs = ", ".join(
            f"max(l.value) FILTER (WHERE l.factor_id = '{f}') AS \"{f}\""
            for f in prm["ts2_factors"])
        return [f"""
            SELECT l.cob_date, l.asset_id, {aggs}
            FROM read_parquet('{p["loading"]}/**/*.parquet') l
            WHERE l.asset_id IN ({_ints(prm["ts2_assets"])})
              AND l.cob_date BETWEEN DATE '{prm["ts2_start"]}' AND DATE '{prm["ts2_end"]}'
              AND l.factor_id IN ({_strs(prm["ts2_factors"])})
            GROUP BY 1, 2 ORDER BY 2, 1
        """]
    f1, f2 = prm["cov_pair"]
    return [_cov_parquet(p, f"factor_id_1 = '{f1}' AND factor_id_2 = '{f2}'",
                         "cob_date, value")]


# ------------------------------------------------- wide transforms (Parquet)

def _read_cs(p: dict) -> str:
    return f"read_parquet('{p['cs']}/**/*.parquet', hive_partitioning=true)"


def _read_ts(p: dict) -> str:
    return f"read_parquet('{p['ts']}/**/*.parquet', hive_partitioning=true)"


def _sql_wide(qid: str, man: dict, layout: str) -> list[str]:
    """layout: 'cs' (partition col year_month) or 'ts' (partition col bucket)."""
    p, prm = _paths(man), man["params"]
    d, part = prm["cs_date"], ("year_month" if layout == "cs" else "bucket")
    src = _read_cs(p) if layout == "cs" else _read_ts(p)
    # Partition predicates: use the layout well where it helps (that's the
    # point of the layout); their absence on the other axis is the measured cost.
    cs_part = f"year_month = '{prm['cs_month']}' AND " if layout == "cs" else ""
    ts1_part = (f"bucket = {prm['ts_asset'] % prm['buckets']} AND "
                if layout == "ts" else "")
    ts2_part = (f"year_month IN ({_strs(prm['ts2_months'])}) AND "
                if layout == "cs" else "")

    if qid == "CS1":
        return [f"SELECT * EXCLUDE ({part}) FROM {src} "
                f"WHERE {cs_part}cob_date = DATE '{d}' ORDER BY asset_id"]
    if qid == "CS2":
        return [f"""
            SELECT w.asset_id, {_q(prm["five_factors"])}
            FROM {src} w
            JOIN read_parquet('{p["mem"]}/**/*.parquet') u
              ON u.cob_date = w.cob_date AND u.asset_id = w.asset_id
             AND u.estimation_universe_flag
            WHERE {cs_part.replace("year_month", "w.year_month")}w.cob_date = DATE '{d}'
              AND u.cob_date = DATE '{d}'
            ORDER BY w.asset_id
        """]
    if qid == "CS3":
        return (_sql_wide("CS1", man, layout)
                + [_cov_parquet(p, f"cob_date = DATE '{d}'",
                                "factor_id_1, factor_id_2, value")])
    if qid == "TS1":
        return [f"SELECT * EXCLUDE ({part}) FROM {src} "
                f"WHERE {ts1_part}asset_id = {prm['ts_asset']} ORDER BY cob_date"]
    if qid == "TS2":
        return [f"""
            SELECT cob_date, asset_id, {_q(prm["ts2_factors"])}
            FROM {src}
            WHERE {ts2_part}asset_id IN ({_ints(prm["ts2_assets"])})
              AND cob_date BETWEEN DATE '{prm["ts2_start"]}' AND DATE '{prm["ts2_end"]}'
            ORDER BY asset_id, cob_date
        """]
    f1, f2 = prm["cov_pair"]
    return [_cov_parquet(p, f"factor_id_1 = '{f1}' AND factor_id_2 = '{f2}'",
                         "cob_date, value")]


# ------------------------------------------- native DuckDB / DuckLake tables

def _sql_table(qid: str, man: dict, layout: str, prefix: str) -> list[str]:
    prm = man["params"]
    d = prm["cs_date"]
    wide = f"{prefix}{'cs_wide' if layout == 'cs' else 'ts_wide'}"
    mem, cov = f"{prefix}membership", f"{prefix}covariance"

    if qid == "CS1":
        return [f"SELECT * FROM {wide} WHERE cob_date = DATE '{d}' ORDER BY asset_id"]
    if qid == "CS2":
        return [f"""
            SELECT w.asset_id, {_q(prm["five_factors"])}
            FROM {wide} w
            JOIN {mem} u ON u.cob_date = w.cob_date AND u.asset_id = w.asset_id
             AND u.estimation_universe_flag
            WHERE w.cob_date = DATE '{d}' ORDER BY w.asset_id
        """]
    if qid == "CS3":
        return (_sql_table("CS1", man, layout, prefix)
                + [f"SELECT factor_id_1, factor_id_2, value FROM {cov} "
                   f"WHERE cob_date = DATE '{d}' ORDER BY cob_date"])
    if qid == "TS1":
        return [f"SELECT * FROM {wide} WHERE asset_id = {prm['ts_asset']} "
                f"ORDER BY cob_date"]
    if qid == "TS2":
        return [f"""
            SELECT cob_date, asset_id, {_q(prm["ts2_factors"])}
            FROM {wide}
            WHERE asset_id IN ({_ints(prm["ts2_assets"])})
              AND cob_date BETWEEN DATE '{prm["ts2_start"]}' AND DATE '{prm["ts2_end"]}'
            ORDER BY asset_id, cob_date
        """]
    f1, f2 = prm["cov_pair"]
    return [f"SELECT cob_date, value FROM {cov} "
            f"WHERE factor_id_1 = '{f1}' AND factor_id_2 = '{f2}' ORDER BY cob_date"]


ARM_IMPLS: dict[str, Arm] = {
    "normalized": Arm("normalized", _connect_memory,
                      lambda q, m: _sql_normalized(q, m)),
    "wide_cs": Arm("wide_cs", _connect_memory,
                   lambda q, m: _sql_wide(q, m, "cs")),
    "wide_ts": Arm("wide_ts", _connect_memory,
                   lambda q, m: _sql_wide(q, m, "ts")),
    "duckdb_cs": Arm("duckdb_cs", _connect_native,
                     lambda q, m: _sql_table(q, m, "cs", "")),
    "duckdb_ts": Arm("duckdb_ts", _connect_native,
                     lambda q, m: _sql_table(q, m, "ts", "")),
    "ducklake_cs": Arm("ducklake_cs", _connect_ducklake,
                       lambda q, m: _sql_table(q, m, "cs", "dl.")),
    "ducklake_ts": Arm("ducklake_ts", _connect_ducklake,
                       lambda q, m: _sql_table(q, m, "ts", "dl.")),
}
