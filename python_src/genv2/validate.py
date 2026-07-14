"""V2 validation (spec §1–§2, §5): fleet counts, variant identity, FMP unit
gross, restatement rate, new-dataset shapes. Blocking."""

from __future__ import annotations

import duckdb

from generator.trading_calendar import trading_days

from .fleet import REGIONS, V2Config


def run_validation(cfg: V2Config) -> int:
    out = cfg.output_dir.rstrip("/")
    con = duckdb.connect()
    if out.startswith("s3://"):
        import os
        con.execute("INSTALL httpfs; LOAD httpfs;")
        con.execute(f"""CREATE SECRET (TYPE s3,
            KEY_ID '{os.environ["AWS_ACCESS_KEY_ID"]}',
            SECRET '{os.environ["AWS_SECRET_ACCESS_KEY"]}',
            REGION 'eu-west-1')""")
    failures = []

    def check(name, ok, detail=""):
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
        if not ok:
            failures.append(name)

    def q(sql, *p):
        return con.execute(sql, list(p)).fetchall()

    for t in ("factor_loading", "specific_risk", "universe_membership",
              "factor_return", "fmp", "asset_return"):
        con.execute(f"CREATE VIEW {t} AS SELECT * FROM "
                    f"read_parquet('{out}/{t}/**/*.parquet', hive_partitioning=true)")
    for t in ("model_master", "factor_master", "asset_master"):
        con.execute(f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{out}/{t}.parquet')")
    con.execute(f"CREATE VIEW restatement_log AS SELECT * FROM "
                f"read_parquet('{out}/restatement_log/**/*.parquet', hive_partitioning=true)")

    days = [r[0] for r in q(
        "SELECT DISTINCT cob_date FROM factor_return ORDER BY cob_date")]
    mid_date = str(days[len(days) // 2])

    # per-region live counts on a sample date
    rows = q(f"""
        SELECT region, count(*) FROM asset_master
        WHERE start_date <= DATE '{mid_date}'
          AND (end_date IS NULL OR end_date >= DATE '{mid_date}')
        GROUP BY region""")
    got = dict(rows)
    gen_regions = {r for m in cfg.models for r in m.universe_regions}
    ok = all(got.get(r) == n for r, n in REGIONS.items() if r in gen_regions)
    check("universe: per-region live counts", ok,
          f"{ {r: got.get(r) for r in sorted(gen_regions)} }")

    # per-model coverage / estu / loading shape on the sample date
    for m in cfg.models:
        cov, estu = q("""
            SELECT count(*), count(*) FILTER (WHERE estimation_universe_flag)
            FROM universe_membership WHERE model_id = ? AND cob_date = ?""",
            m.model_id, mid_date)[0]
        target_cov = sum(REGIONS[r] for r in m.universe_regions) * m.coverage_rate
        check(f"{m.model_id}: coverage≈{target_cov:,.0f}, estu≈{target_cov * m.estu_rate:,.0f}",
              abs(cov - target_cov) < target_cov * 0.03
              and abs(estu - target_cov * m.estu_rate) < target_cov * m.estu_rate * 0.06,
              f"got {cov:,} / {estu:,}")

        n_load, n_fact = q("""
            SELECT count(*), count(DISTINCT factor_id) FROM factor_loading
            WHERE model_id = ? AND cob_date = ? AND version_id = 1""",
            m.model_id, mid_date)[0]
        check(f"{m.model_id}: loading rows/asset in band, factors ≤ {m.n_factors}",
              12 <= n_load / cov <= 24 and n_fact <= m.n_factors,
              f"{n_load / cov:.1f} rows/asset, {n_fact} distinct factors")

    # variant identity: shared-factor loadings byte-identical to base
    for m in cfg.models:
        if not m.base_model_id:
            continue
        shared = m.style_factors[:m.n_base_styles]
        n_diff = q(f"""
            SELECT count(*) FROM
              (SELECT asset_id, factor_id, value FROM factor_loading
               WHERE model_id = ? AND cob_date = ? AND version_id = 1
                 AND factor_id IN ({",".join("?" * len(shared))})) v
            FULL JOIN
              (SELECT asset_id, factor_id, value FROM factor_loading
               WHERE model_id = ? AND cob_date = ? AND version_id = 1
                 AND factor_id IN ({",".join("?" * len(shared))})) b
            USING (asset_id, factor_id)
            WHERE v.value IS DISTINCT FROM b.value""",
            m.model_id, mid_date, *shared, m.base_model_id, mid_date, *shared)[0][0]
        check(f"{m.model_id}: shared style loadings identical to {m.base_model_id}",
              n_diff == 0, f"{n_diff} mismatches")

    # FMP: unit gross per (model, factor) on sample date; factor set = styles+market
    rows = q("""
        SELECT model_id, min(g), max(g) FROM (
          SELECT model_id, factor_id, sum(abs(weight)) g FROM fmp
          WHERE cob_date = ? GROUP BY 1, 2) GROUP BY 1""", mid_date)
    ok = all(abs(lo - 1) < 1e-4 and abs(hi - 1) < 1e-4 for _, lo, hi in rows)
    check("fmp: unit gross per factor", ok, str([(r[0], round(r[1], 5)) for r in rows[:3]]))

    # factor returns: per model-date, n_factors OFFICIAL rows and one
    # T0_ESTIMATE row per FMP factor (styles + market), via the type column
    y = int(mid_date[:4])
    bad = q("""
        SELECT count(*) FROM (
          SELECT model_id, cob_date,
                 count(*) FILTER (WHERE type = 'OFFICIAL') c_off,
                 count(*) FILTER (WHERE type = 'T0_ESTIMATE') c_est
          FROM factor_return WHERE year = ? GROUP BY 1, 2) t
        JOIN model_master mm USING (model_id)
        JOIN (SELECT model_id, count(*) k FROM factor_master
              WHERE factor_type IN ('STYLE', 'MARKET') GROUP BY 1) fm
          USING (model_id)
        WHERE t.c_off <> mm.n_factors OR t.c_est <> fm.k""", y)[0][0]
    check("factor_return: OFFICIAL n_factors + T0 per FMP factor, per model-date",
          bad == 0, f"{bad} bad dates")

    # asset_return: one row per covered asset per model-date
    bad = q("""
        SELECT count(*) FROM (
          SELECT model_id, count(*) c FROM asset_return
          WHERE cob_date = ? GROUP BY 1) a
        JOIN (SELECT model_id, count(*) c FROM universe_membership
              WHERE cob_date = ? GROUP BY 1) u
        USING (model_id) WHERE a.c <> u.c""", mid_date, mid_date)[0][0]
    check("asset_return: rows = coverage per model-date", bad == 0,
          f"{bad} mismatched models")

    # T0 parity: stored estimate == FMP weights × same-day asset returns
    worst = q("""
        SELECT max(abs(est.value - calc.v)
                   / greatest(abs(est.value), 1e-9)) FROM
          (SELECT model_id, factor_id, value FROM factor_return
           WHERE cob_date = ? AND type = 'T0_ESTIMATE') est
        JOIN
          (SELECT f.model_id, f.factor_id, sum(f.weight * r.value) v
           FROM fmp f
           JOIN asset_return r USING (model_id, cob_date, asset_id)
           WHERE f.cob_date = ? GROUP BY 1, 2) calc
        USING (model_id, factor_id)""", mid_date, mid_date)[0][0]
    check("t0 parity: estimate = Σ fmp_weight × asset_return",
          worst is not None and worst < 1e-5, f"worst rel err {worst}")

    # restatement rate ~1%
    for m in cfg.models:
        n = q("SELECT count(*) FROM restatement_log WHERE model_id = ?", m.model_id)[0][0]
        rate = n / len(days)
        check(f"{m.model_id}: restatement rate ≈1%", 0.004 <= rate <= 0.02,
              f"{rate:.3%} ({n} restated dates)")

    if failures:
        print(f"\n{len(failures)} FAILED: {failures}")
        return 1
    print("\nall v2 checks passed")
    return 0
