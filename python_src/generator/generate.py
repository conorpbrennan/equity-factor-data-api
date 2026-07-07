"""Main generation loop (generator-spec.md §4, §6): per model, per year,
sequential per-date stepping with year-boundary checkpoints."""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np

from .config import GeneratorConfig, ModelConfig
from .dimensions import write_dimensions
from .model_state import (
    ModelStatic, advance, build_static, emit_covariance, emit_loadings,
    emit_membership, emit_specific_risk, fresh_state, load_checkpoint,
    save_checkpoint,
)
from .trading_calendar import trading_days, years_of
from .universe import Universe, build_universe
from .writer import (
    covariance_table, loading_table, membership_table, partition_path,
    specific_risk_table, write_parquet,
)


def _generate_model_year(cfg: GeneratorConfig, m: ModelConfig, static: ModelStatic,
                         state, uni: Universe, days: np.ndarray, t_indices: np.ndarray,
                         year: int, out: Path) -> dict[str, int]:
    load_a, load_f, load_v, load_n = [], [], [], []
    cov_v = []
    sr_a, sr_v = [], []
    mem_a, mem_flag, mem_n = [], [], []

    for t in t_indices:
        sigma_ann, sig_ann = advance(state, cfg, m, static, int(t))
        live = uni.live_mask(int(t))

        a, f, v = emit_loadings(state, static, m, live)
        load_a.append(a); load_f.append(f); load_v.append(v); load_n.append(len(v))

        cov_v.append(emit_covariance(sigma_ann, static, m))

        idx, s = emit_specific_risk(sig_ann, static, m, live)
        sr_a.append(idx); sr_v.append(s)

        idx, flag = emit_membership(static, live)
        mem_a.append(idx); mem_flag.append(flag); mem_n.append(len(idx))

    year_days = days[t_indices]
    fids = m.factor_ids
    n_tri = len(static.triu[0])
    n_days = len(t_indices)

    tables = {
        "factor_loading": loading_table(
            np.repeat(year_days, load_n),
            np.concatenate(load_a), np.concatenate(load_f), np.concatenate(load_v),
            fids),
        "factor_covariance": covariance_table(
            np.repeat(year_days, n_tri),
            np.tile(static.triu[0].astype(np.int16), n_days),
            np.tile(static.triu[1].astype(np.int16), n_days),
            np.concatenate(cov_v), fids),
        "specific_risk": specific_risk_table(
            np.repeat(year_days, mem_n),
            np.concatenate(sr_a), np.concatenate(sr_v)),
        "universe_membership": membership_table(
            np.repeat(year_days, mem_n),
            np.concatenate(mem_a), np.concatenate(mem_flag)),
    }
    rows = {}
    for name, table in tables.items():
        write_parquet(table, partition_path(out, name, m.model_id, year), cfg)
        rows[name] = table.num_rows
    return rows


def generate(cfg: GeneratorConfig, years: list[int] | None = None,
             output_dir: str | Path | None = None, quiet: bool = False) -> None:
    """Generate the normalized store. `years` must be a contiguous ascending
    run; any start year after the calendar start resumes from the prior
    year's checkpoint."""
    t0 = time.perf_counter()
    days = trading_days(cfg.start_date, cfg.end_date)
    yr = years_of(days)
    all_years = sorted(set(yr.tolist()))
    sel = all_years if years is None else sorted(years)
    unknown = set(sel) - set(all_years)
    if unknown:
        raise ValueError(f"years outside calendar: {sorted(unknown)}")
    if sel != list(range(sel[0], sel[-1] + 1)):
        raise ValueError("years must be contiguous")

    uni = build_universe(cfg, len(days))
    out = Path(output_dir) if output_dir is not None else Path(cfg.output_dir)
    write_dimensions(cfg, uni, days, out)
    if not quiet:
        print(f"calendar: {len(days)} COB dates {days[0]}..{days[-1]}; "
              f"universe: {uni.n_assets} assets used of {cfg.n_superset} slots")

    for m in cfg.models:
        static = build_static(cfg, m)
        state = None
        for year in sel:
            t_indices = np.flatnonzero(yr == year)
            if state is None:
                state = (fresh_state(cfg, m, static) if t_indices[0] == 0
                         else load_checkpoint(cfg, m, year - 1))
            ty = time.perf_counter()
            rows = _generate_model_year(cfg, m, static, state, uni, days,
                                        t_indices, year, out)
            save_checkpoint(cfg, m, year, state)
            if not quiet:
                print(f"{m.model_id} {year}: "
                      f"{rows['factor_loading']:,} loadings, "
                      f"{rows['factor_covariance']:,} cov, "
                      f"{rows['specific_risk']:,} srisk rows "
                      f"[{time.perf_counter() - ty:.1f}s]")

    if not quiet:
        print(f"done in {time.perf_counter() - t0:.1f}s -> {out}")
