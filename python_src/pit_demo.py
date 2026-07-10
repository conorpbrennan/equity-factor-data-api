"""PIT overlay proof: same as-of result two ways, on generated data.

The generator injects restatements: ~1% of (model, date)s republish that
date's style loadings as version_id=2 rows (restatement_log records the
publication lag). The wide tables materialize version 1 only. This demo
proves, on a restated date for AX_WW4_MH:

  1. the restated values genuinely differ from the originals (not a no-op);
  2. METHOD A (overlay): wide row + LEFT JOIN of the v2 slice + COALESCE
     returns exactly the same as-of cross-section as
     METHOD B (long-form): arg_max(value, version_id) pivot over normalized;
  3. real timings for both.

Usage: PYTHONPATH=python_src python -m pit_demo [--root DIR]
"""

from __future__ import annotations

import argparse
import time

import duckdb

from genv2.fleet import FLEET

M = FLEET["AX_WW4_MH"]
RESTATED_DATE = "2015-02-24"          # from restatement_log (published T+2)
STYLES = M.factor_ids[:M.n_styles]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/home/abrennan/ssd4tb/equity-factor-data-api/v2full")
    args = ap.parse_args()
    root = args.root.rstrip("/")
    loading = f"{root}/normalized/factor_loading/model_id=AX_WW4_MH"
    wide = f"{root}/transforms_a/wide_cs/model_id=AX_WW4_MH"

    con = duckdb.connect()
    con.execute("SET threads = 14; SET memory_limit = '40GB'")

    # -- 1. the restatement is real: v2 differs from v1
    n_diff, n_v2 = con.execute(f"""
        SELECT count(*) FILTER (WHERE v1.value <> v2.value), count(*)
        FROM (SELECT asset_id, factor_id, value FROM read_parquet('{loading}/year=2015/*.parquet')
              WHERE cob_date = DATE '{RESTATED_DATE}' AND version_id = 1) v1
        JOIN (SELECT asset_id, factor_id, value FROM read_parquet('{loading}/year=2015/*.parquet')
              WHERE cob_date = DATE '{RESTATED_DATE}' AND version_id = 2) v2
        USING (asset_id, factor_id)
    """).fetchone()
    print(f"restated cells on {RESTATED_DATE}: {n_v2:,} republished, "
          f"{n_diff:,} changed vs original ({n_diff/n_v2:.0%})")

    # -- 2. the two as-of methods
    over = ", ".join(f"arg_max(l.value, l.version_id) FILTER (WHERE l.factor_id = '{f}') "
                     f'AS "{f}"' for f in STYLES)
    keep = ", ".join(f'coalesce(o."{f}", w."{f}") AS "{f}"' for f in STYLES)
    rest = ", ".join(f'w."{f}"' for f in M.factor_ids[M.n_styles:])
    overlay_sql = f"""
        WITH overlay AS (
            SELECT l.asset_id, {over}
            FROM read_parquet('{loading}/year=2015/*.parquet') l
            WHERE l.cob_date = DATE '{RESTATED_DATE}' AND l.version_id = 2
            GROUP BY 1)
        SELECT w.asset_id, {keep}, {rest}
        FROM read_parquet('{wide}/**/*.parquet', hive_partitioning=true) w
        LEFT JOIN overlay o USING (asset_id)
        WHERE w.year_month = '2015-02' AND w.cob_date = DATE '{RESTATED_DATE}'
        ORDER BY w.asset_id"""

    aggs = []
    for fid, ftype in zip(M.factor_ids, M.factor_types):
        a = f"arg_max(l.value, l.version_id) FILTER (WHERE l.factor_id = '{fid}')"
        if ftype in ("INDUSTRY", "COUNTRY", "CURRENCY"):
            a = f"coalesce({a}, 0.0)"
        aggs.append(f'{a} AS "{fid}"')
    longform_sql = f"""
        SELECT l.asset_id, {", ".join(aggs)}
        FROM read_parquet('{loading}/**/*.parquet') l
        WHERE l.cob_date = DATE '{RESTATED_DATE}'
        GROUP BY 1 ORDER BY 1"""

    def timed(sql, label, n=3):
        con.execute(sql)  # warm
        ts = []
        for _ in range(n):
            t0 = time.perf_counter()
            r = con.execute(sql).to_arrow_table()
            ts.append(time.perf_counter() - t0)
        print(f"{label}: p50 {sorted(ts)[n//2]:.2f}s ({r.num_rows:,} rows)")
        return r

    a = timed(overlay_sql, "METHOD A  wide + v2 overlay  ")
    b = timed(longform_sql, "METHOD B  long-form arg_max ")

    # -- 3. value-identical?
    con.register("ra", a)
    con.register("rb", b)
    checks = " OR ".join(f'ra."{f}" IS DISTINCT FROM rb."{f}"' for f in M.factor_ids)
    n_mismatch = con.execute(
        f"SELECT count(*) FROM ra JOIN rb USING (asset_id) WHERE {checks}").fetchone()[0]
    print(f"as-of results identical: {'YES' if n_mismatch == 0 else f'NO ({n_mismatch} rows differ)'}")


if __name__ == "__main__":
    main()
