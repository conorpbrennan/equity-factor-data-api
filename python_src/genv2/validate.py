"""V2 validation (spec §1–§2, §5): fleet counts, variant identity, FMP unit
gross, restatement rate, new-dataset shapes. Blocking."""

from __future__ import annotations

from pathlib import Path

import duckdb

from generator.trading_calendar import trading_days

from .fleet import REGIONS, V2Config


def run_validation(cfg: V2Config) -> int:
    out = Path(cfg.output_dir)
    con = duckdb.connect()
    failures = []

    def check(name, ok, detail=""):
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
        if not ok:
            failures.append(name)

    def q(sql, *p):
        return con.execute(sql, list(p)).fetchall()

    for t in ("factor_loading", "specific_risk", "universe_membership",
              "factor_return", "fmp"):
        con.execute(f"CREATE VIEW {t} AS SELECT * FROM "
                    f"read_parquet('{out}/{t}/**/*.parquet', hive_partitioning=true)")
    for t in ("model_master", "factor_master", "asset_master", "restatement_log"):
        con.execute(f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{out}/{t}.parquet')")

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

    # factor returns: one row per factor per date (sample year)
    y = int(mid_date[:4])
    bad = q("""
        SELECT count(*) FROM (
          SELECT model_id, cob_date, count(*) c FROM factor_return
          WHERE year = ? GROUP BY 1, 2) t
        JOIN model_master mm USING (model_id) WHERE t.c <> mm.n_factors""", y)[0][0]
    check("factor_return: n_factors rows per model-date", bad == 0, f"{bad} bad dates")

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
