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
RESTATED_DATE = "2015-02-24"       # a v2-restated AX_WW4_MH date (published T+2)
COUNTRY_FACTOR = "CTY03"           # 617 members on CS_DATE
TS2_RANGE = ("2018-01-01", "2022-12-31")
TS2_FACTORS = ("MT_MOMENTUM", "SIZE", "GROWTH")
TS4_FACTOR = "MT_MOMENTUM"
TS4_RANGE = ("2016-01-01", "2025-12-31")
TS5_RANGE = ("2006-01-02", "2025-12-31")
STYLE5 = ("MARKET_SENSITIVITY", "MT_MOMENTUM", "ST_MOMENTUM", "SIZE", "VALUE")

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


    five = ", ".join(f'"{f}"' for f in STYLE5)
    ts2f = ", ".join(f'"{f}"' for f in TS2_FACTORS)
    on_restated = f"cob_date = DATE '{RESTATED_DATE}'"

    if qid == "CS2":     # one date, 5 style factors, estimation universe only
        mem = (f"SELECT asset_id FROM read_parquet('{p['mem']}/**/*.parquet') "
               f"WHERE cob_date = DATE '{CS_DATE}' AND estimation_universe_flag")
        if arm == "C_normalized":
            styles = ", ".join(f"max(l.value) FILTER (WHERE l.factor_id = '{f}') AS \"{f}\"" for f in STYLE5)
            return [f"""
                SELECT l.asset_id, {styles}
                FROM read_parquet('{p["loading"]}/**/*.parquet') l
                WHERE l.cob_date = DATE '{CS_DATE}' AND l.version_id = 1
                  AND l.factor_id IN ({", ".join(f"'{f}'" for f in STYLE5)})
                  AND l.asset_id IN ({mem})
                GROUP BY 1 ORDER BY 1"""]
        return [f"SELECT asset_id, {five} FROM {_wide_cs(p, arm)} "
                f"WHERE cob_date = DATE '{CS_DATE}' AND asset_id IN ({mem}) ORDER BY asset_id"]

    if qid == "CS3":     # full risk-model pull: cross-section + covariance + srisk
        return build("CHAIN2", arm, root, hundred, ts_asset)

    if qid == "CS4":     # one country slice, one date, all factors (global-model regional cut)
        if arm == "C_normalized":
            return [f"""SELECT * FROM ({_pivot(p, on_cs_date)})
                        WHERE "{COUNTRY_FACTOR}" = 1.0 ORDER BY asset_id"""]
        return [f"SELECT * FROM {_wide_cs(p, arm)} "
                f"WHERE cob_date = DATE '{CS_DATE}' AND \"{COUNTRY_FACTOR}\" = 1.0 "
                f"ORDER BY asset_id"]

    if qid == "CS5":     # as-of (PIT) cross-section on a restated date: latest version wins
        if arm == "C_normalized":
            aggs = []
            for fid, ftype in zip(M.factor_ids, M.factor_types):
                a = f"arg_max(l.value, l.version_id) FILTER (WHERE l.factor_id = '{fid}')"
                if ftype in ("INDUSTRY", "COUNTRY", "CURRENCY"):
                    a = f"coalesce({a}, 0.0)"
                aggs.append(f'{a} AS "{fid}"')
            return [f"""
                SELECT l.asset_id, {", ".join(aggs)}
                FROM read_parquet('{p["loading"]}/**/*.parquet') l
                WHERE l.cob_date = DATE '{RESTATED_DATE}'
                GROUP BY 1 ORDER BY 1"""]
        # wide layouts materialize v1 only: base from wide + style overlay from normalized v2
        styles = M.factor_ids[:M.n_styles]
        over = ", ".join(f"arg_max(l.value, l.version_id) FILTER (WHERE l.factor_id = '{f}') AS \"{f}\"" for f in styles)
        keep = ", ".join(f'coalesce(o."{f}", w."{f}") AS "{f}"' for f in styles)
        rest = ", ".join(f'w."{f}"' for f in M.factor_ids[M.n_styles:])
        return [f"""
            WITH overlay AS (
                SELECT l.asset_id, {over}
                FROM read_parquet('{p["loading"]}/**/*.parquet') l
                WHERE l.cob_date = DATE '{RESTATED_DATE}' AND l.version_id = 2
                GROUP BY 1
            )
            SELECT w.asset_id, {keep}, {rest}, w.specific_risk
            FROM {_wide_cs(p, arm)} w
            LEFT JOIN overlay o USING (asset_id)
            WHERE w.cob_date = DATE '{RESTATED_DATE}'
            ORDER BY w.asset_id"""]

    if qid == "CS6":     # screen: top-decile momentum within estu, one date, styles returned
        mem = (f"SELECT asset_id FROM read_parquet('{p['mem']}/**/*.parquet') "
               f"WHERE cob_date = DATE '{CS_DATE}' AND estimation_universe_flag")
        if arm == "C_normalized":
            src = f"({_pivot(p, on_cs_date)})"
        else:
            src = f"(SELECT * FROM {_wide_cs(p, arm)} WHERE cob_date = DATE '{CS_DATE}')"
        return [f"""
            SELECT asset_id, {five}
            FROM {src}
            WHERE asset_id IN ({mem})
            ORDER BY "MT_MOMENTUM" DESC LIMIT 1300"""]

    if qid == "TS2":     # 100 assets, 5 years, 3 factors (mixed/awkward)
        if arm == "C_normalized":
            styles = ", ".join(f"max(l.value) FILTER (WHERE l.factor_id = '{f}') AS \"{f}\"" for f in TS2_FACTORS)
            return [f"""
                SELECT l.cob_date, l.asset_id, {styles}
                FROM read_parquet('{p["loading"]}/**/*.parquet') l
                WHERE l.asset_id IN ({ids}) AND l.version_id = 1
                  AND l.cob_date BETWEEN DATE '{TS2_RANGE[0]}' AND DATE '{TS2_RANGE[1]}'
                  AND l.factor_id IN ({", ".join(f"'{f}'" for f in TS2_FACTORS)})
                GROUP BY 1, 2 ORDER BY 2, 1"""]
        return [f"""
            SELECT cob_date, asset_id, {ts2f} FROM {_wide_ts(p, arm)}
            WHERE asset_id IN ({ids})
              AND cob_date BETWEEN DATE '{TS2_RANGE[0]}' AND DATE '{TS2_RANGE[1]}'
            ORDER BY asset_id, cob_date"""]

    if qid == "TS3":     # covariance series, one pair, 20y — shared table, all arms
        return [f"SELECT cob_date, value FROM read_parquet('{p['cov']}/**/*.parquet') "
                f"WHERE factor_id_1 = 'MARKET_SENSITIVITY' AND factor_id_2 = 'MT_MOMENTUM' "
                f"ORDER BY cob_date"]

    if qid == "TS4":     # factor-research panel: one factor, whole estu, month-ends, 10y
        if arm == "C_normalized":
            src = f"""
                SELECT l.cob_date, l.asset_id, l.value AS x
                FROM read_parquet('{p["loading"]}/**/*.parquet') l
                WHERE l.factor_id = '{TS4_FACTOR}' AND l.version_id = 1
                  AND l.cob_date BETWEEN DATE '{TS4_RANGE[0]}' AND DATE '{TS4_RANGE[1]}'"""
        else:
            src = f"""
                SELECT cob_date, asset_id, "{TS4_FACTOR}" AS x FROM {_wide_cs(p, arm)}
                WHERE cob_date BETWEEN DATE '{TS4_RANGE[0]}' AND DATE '{TS4_RANGE[1]}'"""
        return [f"""
            WITH panel AS ({src})
            SELECT * FROM panel
            WHERE cob_date IN (
                SELECT max(cob_date) FROM panel GROUP BY date_trunc('month', cob_date))
            ORDER BY cob_date, asset_id"""]

    if qid == "TS5":     # tall-thin: 10 assets, 2 columns, 20 years
        ten = ", ".join(map(str, hundred[:10]))
        if arm == "C_normalized":
            return [f"""
                SELECT l.cob_date, l.asset_id,
                       max(l.value) FILTER (WHERE l.factor_id = '{TS4_FACTOR}') AS mom,
                       max(s.value) AS specific_risk
                FROM read_parquet('{p["loading"]}/**/*.parquet') l
                JOIN read_parquet('{p["srisk"]}/**/*.parquet') s
                  ON s.cob_date = l.cob_date AND s.asset_id = l.asset_id AND s.version_id = 1
                WHERE l.asset_id IN ({ten}) AND l.version_id = 1
                  AND l.factor_id = '{TS4_FACTOR}'
                GROUP BY 1, 2 ORDER BY 2, 1"""]
        return [f"""
            SELECT cob_date, asset_id, "{TS4_FACTOR}", specific_risk
            FROM {_wide_ts(p, arm)} WHERE asset_id IN ({ten})
            ORDER BY asset_id, cob_date"""]

    if qid == "TS6":     # as-of (PIT) single-asset history: latest version wins
        if arm == "C_normalized":
            aggs = []
            for fid, ftype in zip(M.factor_ids, M.factor_types):
                a = f"arg_max(l.value, l.version_id) FILTER (WHERE l.factor_id = '{fid}')"
                if ftype in ("INDUSTRY", "COUNTRY", "CURRENCY"):
                    a = f"coalesce({a}, 0.0)"
                aggs.append(f'{a} AS "{fid}"')
            return [f"""
                SELECT l.cob_date, {", ".join(aggs)}
                FROM read_parquet('{p["loading"]}/**/*.parquet') l
                WHERE l.asset_id = {ts_asset}
                GROUP BY 1 ORDER BY 1"""]
        styles = M.factor_ids[:M.n_styles]
        over = ", ".join(f"arg_max(l.value, l.version_id) FILTER (WHERE l.factor_id = '{f}') AS \"{f}\"" for f in styles)
        keep = ", ".join(f'coalesce(o."{f}", w."{f}") AS "{f}"' for f in styles)
        rest = ", ".join(f'w."{f}"' for f in M.factor_ids[M.n_styles:])
        return [f"""
            WITH overlay AS (
                SELECT l.cob_date, {over}
                FROM read_parquet('{p["loading"]}/**/*.parquet') l
                WHERE l.asset_id = {ts_asset} AND l.version_id = 2
                GROUP BY 1
            )
            SELECT w.cob_date, {keep}, {rest}, w.specific_risk
            FROM {_wide_ts(p, arm)} w
            LEFT JOIN overlay o USING (cob_date)
            WHERE w.asset_id = {ts_asset}
            ORDER BY w.cob_date"""]

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
