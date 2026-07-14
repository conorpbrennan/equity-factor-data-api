"""Replay stage: (re)build factor_return + asset_return for an existing store.

Replays the deterministic state evolution from 2006 (fresh state — no
checkpoints needed) and writes ONLY the two return facts: factor_return
regenerated with the type column (OFFICIAL rows numerically identical to the
original build, T0_ESTIMATE rows new) and the new asset_return fact. The big
facts (loadings, fmp, srisk, covariance, membership) are recomputed in memory
because asset returns and estimates are derived from them, but never written.

factor_return is replaced wholesale — the schema changes, and old/new files
mixed in one partition would break reads — so each model's prefix is deleted
before its rewrite (the transforms _fresh_dir lesson).
"""

from __future__ import annotations

import time

import numpy as np

from generator.trading_calendar import trading_days, years_of

from . import writer as W
from .engine import (advance, build_static, emit_asset_returns,
                     emit_factor_returns, emit_fmp, emit_loadings,
                     emit_t0_estimates, fresh_state)
from .fleet import V2Config
from .transforms import _fresh_dir
from .universe import build_multi_universe


def generate_returns(cfg: V2Config, quiet: bool = False) -> None:
    """Full-range replay for cfg.models; writes to cfg.output_dir."""
    t0 = time.perf_counter()
    days = trading_days(cfg.start_date, cfg.end_date)
    yr = years_of(days)
    uni = build_multi_universe(cfg, len(days))
    out = cfg.output_dir.rstrip("/")
    S = uni.n_slots

    for m in cfg.models:
        _fresh_dir(f"{out}/factor_return/model_id={m.model_id}")
        _fresh_dir(f"{out}/asset_return/model_id={m.model_id}")
        static = build_static(cfg, m, uni)
        state = fresh_state(m, static, S)

        # identical chunking decision to generate(), so file names line up
        live0 = uni.live_mask(0)
        n_c = int((live0 & static.covered).sum())
        nnz = (m.n_styles + 1.1 + (1 if m.n_countries else 0)
               + (1 if m.n_currencies else 0) + 1)
        monthly = n_c * nnz * 261 > cfg.monthly_chunk_threshold

        for year in sorted(set(yr.tolist())):
            tsel = np.flatnonzero(yr == year)
            ty = time.perf_counter()
            months = (days[tsel].astype("datetime64[M]").astype(int) % 12
                      if monthly else np.zeros(len(tsel), int))
            for chunk_id in sorted(set(months.tolist())):
                r_dates, r_vals, est_vals = [], [], []
                ar = ([], [], [])
                for t in tsel[months == chunk_id]:
                    t = int(t)
                    sigma, sig = advance(state, cfg, m, static, t, S)
                    live = uni.live_mask(t)
                    day = days[t]

                    a, f, v = emit_loadings(state, static, m, live)
                    r_dates.append(day)
                    r_vals.append(emit_factor_returns(state, static, m))
                    ff, fa, fv = emit_fmp(state, static, m, live)
                    ai, av = emit_asset_returns(state, static, m, cfg, live,
                                                sig, t, a, f, v)
                    ar[0].append(np.repeat(day, len(ai)))
                    ar[1].append(ai); ar[2].append(av)
                    est_vals.append(emit_t0_estimates(m, ff, fa, fv, ai, av, S))

                dates = np.asarray(r_dates)
                nd, nf = len(dates), m.n_factors
                seqs_est = np.asarray(m.fmp_factor_seq, dtype=np.int16)
                k = len(seqs_est)
                fr = W.freturn_table(
                    np.concatenate([np.repeat(dates, nf), np.repeat(dates, k)]),
                    np.concatenate([np.tile(np.arange(nf, dtype=np.int16), nd),
                                    np.tile(seqs_est, nd)]),
                    np.concatenate([np.concatenate(r_vals),
                                    np.concatenate(est_vals)]),
                    m.factor_ids,
                    np.concatenate([np.zeros(nd * nf, np.int8),
                                    np.ones(nd * k, np.int8)]))
                W.write_chunk(fr, out, "factor_return", m.model_id,
                              year, chunk_id, cfg)
                at = W.areturn_table(np.concatenate(ar[0]),
                                     np.concatenate(ar[1]),
                                     np.concatenate(ar[2]))
                W.write_chunk(at, out, "asset_return", m.model_id,
                              year, chunk_id, cfg)
            if not quiet:
                print(f"{m.model_id} {year}: returns replayed "
                      f"[{time.perf_counter() - ty:.1f}s]", flush=True)
    if not quiet:
        print(f"done in {(time.perf_counter() - t0) / 60:.1f} min -> {out}")
