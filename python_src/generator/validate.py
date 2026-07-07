"""Validation suite (generator-spec.md §8). Blocking: exit nonzero on failure."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

import duckdb
import numpy as np

from .config import GeneratorConfig
from .model_state import build_static
from .rng import stream
from .trading_calendar import trading_days, years_of
from .universe import build_universe
from .writer import loading_table, partition_path, write_parquet

FACT_TABLES = ("factor_loading", "factor_covariance", "specific_risk", "universe_membership")
DIM_TABLES = ("model_master", "factor_master", "asset_master", "asset_xref")


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
        if not ok:
            self.failures.append(name)


def _connect(data: Path) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    for t in FACT_TABLES:
        con.execute(
            f"CREATE VIEW {t} AS SELECT * FROM "
            f"read_parquet('{data}/{t}/**/*.parquet', hive_partitioning=true)")
    for t in DIM_TABLES:
        con.execute(f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{data}/{t}.parquet')")
    return con


def _scalar(con, sql: str, *params):
    return con.execute(sql, params).fetchone()[0]


def _check_universe(rep: Report, con, cfg: GeneratorConfig, days: np.ndarray) -> None:
    sample = days[::25]
    dates_sql = ",".join(f"(DATE '{d}')" for d in sample)
    rows = con.execute(
        f"SELECT count(*) FROM (VALUES {dates_sql}) t(d) "
        f"JOIN asset_master a ON a.start_date <= t.d "
        f"AND (a.end_date IS NULL OR a.end_date >= t.d) GROUP BY t.d").fetchall()
    counts = {r[0] for r in rows}
    rep.check("universe: live count constant", counts == {cfg.n_live},
              f"{len(sample)} sampled dates, counts={sorted(counts)}")

    n_bad = _scalar(con,
        "SELECT count(*) FROM universe_membership u JOIN asset_master a USING (asset_id) "
        "WHERE u.cob_date < a.start_date "
        "OR (a.end_date IS NOT NULL AND u.cob_date > a.end_date)")
    rep.check("universe: membership within asset lifecycle", n_bad == 0,
              f"{n_bad} violations")

    rows = con.execute(
        "SELECT model_id, min(c), max(c) FROM (SELECT model_id, cob_date, count(*) c "
        "FROM universe_membership GROUP BY 1, 2) GROUP BY 1").fetchall()
    ok = all(2850 <= lo and hi <= cfg.n_live for _, lo, hi in rows)
    rep.check("universe: covered count per date in band", ok, str(rows))


def _check_persistence(rep: Report, con, cfg: GeneratorConfig) -> None:
    for m in cfg.models:
        res = con.execute(
            "SELECT asset_id, value FROM factor_loading "
            "WHERE model_id = ? AND factor_id = ? AND asset_id <= 50 "
            "ORDER BY asset_id, cob_date",
            [m.model_id, m.style_factors[0]]).fetchnumpy()
        aid, val = res["asset_id"], res["value"]
        corrs, weights = [], []
        for a in np.unique(aid):
            x = val[aid == a]
            if len(x) > 100:
                corrs.append(np.corrcoef(x[:-1], x[1:])[0, 1])
                weights.append(len(x))
        # Sample lag-1 autocorr is biased low by ~(1 + 3*rho)/n; the lower
        # bound tolerates that at short calendars while still rejecting i.i.d.
        pooled = float(np.average(corrs, weights=weights))
        rep.check(f"persistence: {m.model_id} style lag-1 autocorr",
                  0.98 <= pooled <= 0.995, f"{pooled:.4f} over {len(corrs)} assets")

        switches, n_obs = con.execute(
            "WITH prim AS ("
            " SELECT l.asset_id, l.cob_date, fm.factor_seq"
            " FROM factor_loading l"
            " JOIN factor_master fm ON fm.model_id = l.model_id"
            "  AND fm.factor_id = l.factor_id"
            " WHERE l.model_id = ? AND fm.factor_type = 'INDUSTRY' AND l.value > 0.5"
            "), flag AS ("
            " SELECT factor_seq, lag(factor_seq) OVER"
            "  (PARTITION BY asset_id ORDER BY cob_date) AS prev FROM prim) "
            "SELECT count(*) FILTER (WHERE prev IS NOT NULL AND factor_seq <> prev), "
            "count(*) FROM flag", [m.model_id]).fetchone()
        rate = switches / (n_obs / 261.0)
        rep.check(f"persistence: {m.model_id} industry switch rate ~1%/yr",
                  0.005 <= rate <= 0.02, f"{rate:.4f}/yr ({switches} switches)")


def _check_cross_section(rep: Report, con, cfg: GeneratorConfig, days: np.ndarray) -> None:
    sample = days[::500]
    dates_sql = ",".join(f"DATE '{d}'" for d in sample)
    for m in cfg.models:
        rows = con.execute(
            f"SELECT avg(l.value) mu, stddev_pop(l.value) sd "
            f"FROM factor_loading l "
            f"JOIN universe_membership u ON u.model_id = l.model_id "
            f"AND u.cob_date = l.cob_date AND u.asset_id = l.asset_id "
            f"AND u.estimation_universe_flag "
            f"JOIN factor_master fm ON fm.model_id = l.model_id AND fm.factor_id = l.factor_id "
            f"WHERE l.model_id = ? AND fm.factor_type = 'STYLE' "
            f"AND l.cob_date IN ({dates_sql}) "
            f"GROUP BY l.cob_date, l.factor_id", [m.model_id]).fetchall()
        worst_mu = max(abs(r[0]) for r in rows)
        worst_sd = max(abs(r[1] - 1.0) for r in rows)
        rep.check(f"cross-section: {m.model_id} styles ~ z-scores over ESTU",
                  worst_mu < 0.02 and worst_sd < 0.02,
                  f"max|mean|={worst_mu:.4f}, max|sd-1|={worst_sd:.4f}, "
                  f"{len(rows)} (date, factor) cells")


def _check_covariance(rep: Report, con, cfg: GeneratorConfig,
                      all_years: list[int], full: bool) -> None:
    for m in cfg.models:
        n_tri = m.n_factors * (m.n_factors + 1) // 2
        rows = con.execute(
            "SELECT min(c), max(c) FROM (SELECT cob_date, count(*) c "
            "FROM factor_covariance WHERE model_id = ? GROUP BY 1)",
            [m.model_id]).fetchone()
        rep.check(f"covariance: {m.model_id} triangle count == {n_tri}",
                  rows == (n_tri, n_tri), f"min/max per date = {rows}")

        seq = {fid: i for i, fid in enumerate(m.factor_ids)}
        years = all_years if full else [all_years[len(all_years) // 2]]
        worst = np.inf
        for y in years:
            res = con.execute(
                "SELECT cob_date, factor_id_1, factor_id_2, value "
                "FROM factor_covariance WHERE model_id = ? AND year = ?",
                [m.model_id, y]).fetch_arrow_table()
            dates = res["cob_date"].to_numpy()
            i1 = np.array([seq[f] for f in res["factor_id_1"].to_pylist()])
            i2 = np.array([seq[f] for f in res["factor_id_2"].to_pylist()])
            v = res["value"].to_numpy()
            uniq, dcode = np.unique(dates, return_inverse=True)
            F = m.n_factors
            mats = np.zeros((len(uniq), F, F))
            mats[dcode, i1, i2] = v
            mats[dcode, i2, i1] = v
            worst = min(worst, float(np.linalg.eigvalsh(mats).min()))
        rep.check(f"covariance: {m.model_id} PSD "
                  f"({'all years' if full else f'year {years[0]}'})",
                  worst > 0.0, f"min eigenvalue = {worst:.3e}")


def _check_conventions(rep: Report, con, cfg: GeneratorConfig) -> None:
    for m in cfg.models:
        lo_ann, hi_ann = m.srisk_clip_ann
        if m.specific_risk_convention == "ann_vol_pct":
            band = (lo_ann * 100.0, hi_ann * 100.0)
        else:
            band = (lo_ann / np.sqrt(252.0), hi_ann / np.sqrt(252.0))
        mn, mx = con.execute(
            "SELECT min(value), max(value) FROM specific_risk WHERE model_id = ?",
            [m.model_id]).fetchone()
        ok = band[0] * 0.999 <= mn and mx <= band[1] * 1.001
        rep.check(f"conventions: {m.model_id} specific risk in "
                  f"{m.specific_risk_convention} band", ok,
                  f"[{mn:.4g}, {mx:.4g}] vs [{band[0]:.4g}, {band[1]:.4g}]")


def _check_referential_integrity(rep: Report, con) -> None:
    n = _scalar(con,
        "SELECT count(*) FROM ("
        " SELECT DISTINCT model_id, factor_id FROM factor_loading"
        " UNION SELECT DISTINCT model_id, factor_id_1 FROM factor_covariance"
        " UNION SELECT DISTINCT model_id, factor_id_2 FROM factor_covariance) f "
        "LEFT JOIN factor_master fm USING (model_id, factor_id) "
        "WHERE fm.factor_type IS NULL")
    rep.check("integrity: all fact factor_ids in factor_master", n == 0, f"{n} orphans")

    n = _scalar(con,
        "SELECT count(*) FROM ("
        " SELECT DISTINCT asset_id FROM factor_loading"
        " UNION SELECT DISTINCT asset_id FROM specific_risk"
        " UNION SELECT DISTINCT asset_id FROM universe_membership) x "
        "LEFT JOIN asset_master a USING (asset_id) WHERE a.ticker IS NULL")
    rep.check("integrity: all fact asset_ids in asset_master", n == 0, f"{n} orphans")

    n = _scalar(con,
        "SELECT count(*) FROM ("
        " SELECT DISTINCT model_id FROM factor_loading"
        " UNION SELECT DISTINCT model_id FROM universe_membership) x "
        "LEFT JOIN model_master mm USING (model_id) WHERE mm.vendor IS NULL")
    rep.check("integrity: all fact model_ids in model_master", n == 0, f"{n} orphans")


def _check_determinism(rep: Report, cfg: GeneratorConfig, data: Path, year: int) -> None:
    from .generate import generate  # deferred: avoids import cycle
    with tempfile.TemporaryDirectory(prefix="regen-") as tmp:
        generate(cfg, years=[year], output_dir=tmp, quiet=True)
        mismatches = []
        for m in cfg.models:
            for t in FACT_TABLES:
                orig = partition_path(data, t, m.model_id, year)
                regen = partition_path(Path(tmp), t, m.model_id, year)
                h1 = hashlib.sha256(orig.read_bytes()).hexdigest()
                h2 = hashlib.sha256(regen.read_bytes()).hexdigest()
                if h1 != h2:
                    mismatches.append(f"{t}/{m.model_id}")
        rep.check(f"determinism: year {year} regenerates byte-identical",
                  not mismatches, ", ".join(mismatches) or
                  f"{4 * len(cfg.models)} files match")


def _check_compression_control(rep: Report, cfg: GeneratorConfig, data: Path,
                               days: np.ndarray, year: int) -> None:
    """Spec §8.7: one month of dense i.i.d. noise (the strawman generator from
    plan §1) vs the real sparse/AR/rounded data. Primary metric is bytes per
    asset-date (sparsity is the dominant realism effect); bytes per row must
    also beat i.i.d. (value-entropy effect of AR + 7-sig-digit rounding)."""
    import pyarrow.parquet as pq

    m = cfg.models[0]
    uni = build_universe(cfg, len(days))
    static = build_static(cfg, m)
    yr = years_of(days)
    t_indices = np.flatnonzero(yr == year)[:21]
    F = m.n_factors

    dates_rep, aa, ff, vv = [], [], [], []
    for t in t_indices:
        idx = np.flatnonzero(uni.live_mask(int(t)) & static.covered).astype(np.int32)
        g = stream(cfg.global_seed, m.model_id, "iid_control", int(t))
        aa.append(np.repeat(idx, F))
        ff.append(np.tile(np.arange(F, dtype=np.int16), len(idx)))
        vv.append(g.normal(size=len(idx) * F))
        dates_rep.append(np.repeat(days[t], len(idx) * F))
    control = loading_table(np.concatenate(dates_rep), np.concatenate(aa),
                            np.concatenate(ff), np.concatenate(vv), m.factor_ids)

    with tempfile.TemporaryDirectory(prefix="ctl-") as tmp:
        ctl_path = Path(tmp) / "control.parquet"
        write_parquet(control, ctl_path, cfg)
        ctl_bpr = ctl_path.stat().st_size / control.num_rows
        ctl_bpad = ctl_path.stat().st_size / (control.num_rows / F)

    actual = partition_path(data, "factor_loading", m.model_id, year)
    actual_bpr = actual.stat().st_size / pq.ParquetFile(actual).metadata.num_rows
    n_asset_dates = pq.ParquetFile(
        partition_path(data, "universe_membership", m.model_id, year)).metadata.num_rows
    actual_bpad = actual.stat().st_size / n_asset_dates

    r_ad = actual_bpad / ctl_bpad
    r_row = actual_bpr / ctl_bpr
    rep.check("compression: realistic data beats dense i.i.d. control",
              r_ad < 0.5 and r_row < 0.95,
              f"per asset-date {actual_bpad:.0f}B vs {ctl_bpad:.0f}B (ratio {r_ad:.2f}); "
              f"per row {actual_bpr:.2f}B vs {ctl_bpr:.2f}B (ratio {r_row:.2f})")


def run_validation(cfg: GeneratorConfig, data_dir: str | Path | None = None,
                   full: bool = False, determinism_year: int | None = None,
                   compression_control: bool = False) -> int:
    data = Path(data_dir) if data_dir is not None else Path(cfg.output_dir)
    days = trading_days(cfg.start_date, cfg.end_date)
    all_years = sorted(set(years_of(days).tolist()))
    rep = Report()
    con = _connect(data)

    _check_universe(rep, con, cfg, days)
    _check_persistence(rep, con, cfg)
    _check_cross_section(rep, con, cfg, days)
    _check_covariance(rep, con, cfg, all_years, full)
    _check_conventions(rep, con, cfg)
    _check_referential_integrity(rep, con)
    if determinism_year is not None:
        _check_determinism(rep, cfg, data, determinism_year)
    if compression_control:
        _check_compression_control(rep, cfg, data, days,
                                   all_years[len(all_years) // 2])

    if rep.failures:
        print(f"\n{len(rep.failures)} check(s) FAILED: {', '.join(rep.failures)}")
        return 1
    print("\nall checks passed")
    return 0
