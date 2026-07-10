"""Query builders per arm. Each returns an ordered list of SQL statements —
chains are multi-statement sessions, timed end to end."""

from __future__ import annotations

from genv2.fleet import FLEET

MODEL = "AX_WW4_MH"
M = FLEET[MODEL]
CS_DATE = "2016-06-15"
LAST_DATE = "2025-12-31"
CHAIN1_RET_RANGE = ("2021-01-01", "2025-12-31")
FMP_FACTOR = "MT_MOMENTUM"
FMP2_RANGE = ("2006-01-02", "2025-12-31")

_FACTOR_COLS = ", ".join(f'"{f}"' for f in M.factor_ids)


def paths(root: str) -> dict:
    root = root.rstrip("/")
    return {
        "wide_cs": f"{root}/transforms_a/wide_cs/model_id={MODEL}",
        "wide_ts": f"{root}/transforms_a/wide_ts/model_id={MODEL}",
        "gen_cs": f"{root}/transforms_b/generic_cs/model_id={MODEL}",
        "gen_ts": f"{root}/transforms_b/generic_ts/model_id={MODEL}",
        "loading": f"{root}/normalized/factor_loading/model_id={MODEL}",
        "srisk": f"{root}/normalized/specific_risk/model_id={MODEL}",
        "mem": f"{root}/normalized/universe_membership/model_id={MODEL}",
        "cov": f"{root}/normalized/factor_covariance/model_id={MODEL}",
        "fret": f"{root}/normalized/factor_return/model_id={MODEL}",
        "fmp": f"{root}/normalized/fmp/model_id={MODEL}",
    }


def _wide_cs(p, arm):
    """The date-major relation for an arm: named columns either physical (A)
    or via slot aliasing exactly as the generated views define it (B)."""
    if arm == "A_permodel":
        return f"read_parquet('{p['wide_cs']}/**/*.parquet', hive_partitioning=true)"
    named = ", ".join(f'F{seq + 1:03d} AS "{fid}"' for seq, fid in enumerate(M.factor_ids))
    return (f"(SELECT cob_date, asset_id, {named}, specific_risk "
            f"FROM read_parquet('{p['gen_cs']}/**/*.parquet', hive_partitioning=true))")


def _wide_ts(p, arm):
    if arm == "A_permodel":
        return f"read_parquet('{p['wide_ts']}/**/*.parquet', hive_partitioning=true)"
    named = ", ".join(f'F{seq + 1:03d} AS "{fid}"' for seq, fid in enumerate(M.factor_ids))
    return (f"(SELECT cob_date, asset_id, {named}, specific_risk "
            f"FROM read_parquet('{p['gen_ts']}/**/*.parquet', hive_partitioning=true))")


def _pivot(p, where):
    aggs = []
    for fid, ftype in zip(M.factor_ids, M.factor_types):
        a = f"max(l.value) FILTER (WHERE l.factor_id = '{fid}')"
        if ftype in ("INDUSTRY", "COUNTRY", "CURRENCY"):
            a = f"coalesce({a}, 0.0)"
        aggs.append(f'{a} AS "{fid}"')
    return f"""
        SELECT l.cob_date, l.asset_id, {", ".join(aggs)}
        FROM read_parquet('{p["loading"]}/**/*.parquet') l
        WHERE {where} AND l.version_id = 1
        GROUP BY 1, 2
    """


def build(qid: str, arm: str, root: str, hundred: list[int], ts_asset: int) -> list[str]:
    p = paths(root)
    ids = ", ".join(map(str, hundred))

    on_cs_date = f"cob_date = DATE '{CS_DATE}'"
    on_last_date = f"cob_date = DATE '{LAST_DATE}'"

    if qid == "CS1":
        if arm == "C_normalized":
            return [f"SELECT * FROM ({_pivot(p, on_cs_date)}) ORDER BY asset_id"]
        return [f"SELECT * FROM {_wide_cs(p, arm)} "
                f"WHERE cob_date = DATE '{CS_DATE}' ORDER BY asset_id"]

    if qid == "TS1":
        if arm == "C_normalized":
            return [f"SELECT * FROM ({_pivot(p, f'asset_id = {ts_asset}')}) ORDER BY cob_date"]
        return [f"SELECT * FROM {_wide_ts(p, arm)} "
                f"WHERE asset_id = {ts_asset} ORDER BY cob_date"]

    if qid == "CHAIN1":   # loadings for 100 names, latest date -> returns 5y
        if arm == "C_normalized":
            first = (f"SELECT * FROM ({_pivot(p, on_last_date)}) "
                     f"WHERE asset_id IN ({ids}) ORDER BY asset_id")
        else:
            first = (f"SELECT * FROM {_wide_cs(p, arm)} "
                     f"WHERE cob_date = DATE '{LAST_DATE}' AND asset_id IN ({ids}) "
                     f"ORDER BY asset_id")
        return [first,
                f"SELECT cob_date, factor_id, value "
                f"FROM read_parquet('{p['fret']}/**/*.parquet') "
                f"WHERE cob_date BETWEEN DATE '{CHAIN1_RET_RANGE[0]}' AND DATE '{CHAIN1_RET_RANGE[1]}' "
                f"ORDER BY factor_id, cob_date"]

    if qid == "CHAIN2":   # estu loadings one date -> covariance -> specific risk
        if arm == "C_normalized":
            first = f"""
                SELECT w.* FROM ({_pivot(p, on_cs_date)}) w
                JOIN read_parquet('{p["mem"]}/**/*.parquet') u
                  ON u.cob_date = w.cob_date AND u.asset_id = w.asset_id
                 AND u.estimation_universe_flag
                ORDER BY w.asset_id"""
            third = (f"SELECT asset_id, value FROM read_parquet('{p['srisk']}/**/*.parquet') "
                     f"WHERE cob_date = DATE '{CS_DATE}' AND version_id = 1 ORDER BY asset_id")
        else:
            first = f"""
                SELECT w.* FROM {_wide_cs(p, arm)} w
                JOIN read_parquet('{p["mem"]}/**/*.parquet') u
                  ON u.cob_date = w.cob_date AND u.asset_id = w.asset_id
                 AND u.estimation_universe_flag
                WHERE w.cob_date = DATE '{CS_DATE}'
                ORDER BY w.asset_id"""
            third = (f"SELECT asset_id, specific_risk FROM {_wide_cs(p, arm)} "
                     f"WHERE cob_date = DATE '{CS_DATE}' ORDER BY asset_id")
        return [first,
                f"SELECT factor_id_1, factor_id_2, value "
                f"FROM read_parquet('{p['cov']}/**/*.parquet') "
                f"WHERE cob_date = DATE '{CS_DATE}'",
                third]

    if qid == "FMP1":     # shared: one factor's full weight vector, one date
        return [f"SELECT asset_id, weight FROM read_parquet('{p['fmp']}/**/*.parquet') "
                f"WHERE cob_date = DATE '{CS_DATE}' AND factor_id = '{FMP_FACTOR}' "
                f"ORDER BY asset_id"]

    if qid == "FMP2":     # shared: one factor's weights for 10 assets, 20y
        ten = ", ".join(map(str, hundred[:10]))
        return [f"SELECT cob_date, asset_id, weight "
                f"FROM read_parquet('{p['fmp']}/**/*.parquet') "
                f"WHERE factor_id = '{FMP_FACTOR}' AND asset_id IN ({ten}) "
                f"ORDER BY asset_id, cob_date"]

    raise ValueError(qid)
