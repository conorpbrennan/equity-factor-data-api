"""V2 generation pipeline: fleet loop, monthly chunking for global-scale
models, restatement injection (spec §5), new datasets, v2 dimensions."""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pyarrow as pa

from generator.rng import round_sig, stream
from generator.trading_calendar import trading_days, years_of
from generator.writer import write_parquet

from . import writer as W
from .engine import (ModelStatic, ModelState, advance, build_static,
                     emit_covariance, emit_factor_returns, emit_fmp,
                     emit_loadings, emit_membership, emit_specific_risk,
                     fresh_state, load_checkpoint, save_checkpoint)
from .fleet import V2Config, V2Model
from .universe import MultiUniverse, build_multi_universe

SECTORS = ("ENERGY", "MATERIALS", "INDUSTRIALS", "CONS_DISC", "CONS_STAPLES",
           "HEALTH_CARE", "FINANCIALS", "INFO_TECH", "COMM_SVCS", "UTILITIES",
           "REAL_ESTATE")
RESTATE_STYLE_SD = 0.02
RESTATE_SRISK_SD = 0.01


def _ticker(aid: int) -> str:
    n, s = aid - 1, ""
    for _ in range(4):
        s = chr(65 + n % 26) + s
        n //= 26
    return s


def write_dimensions(cfg: V2Config, uni: MultiUniverse, days, out: Path) -> None:
    from .fleet import FLEET
    ms = tuple(FLEET.values())   # dims always describe the full fleet
    write_parquet(pa.table({
        "model_id": [m.model_id for m in ms],
        "vendor": [m.vendor for m in ms],
        "model_name": [m.model_name for m in ms],
        "variant": [m.variant for m in ms],
        "region": ["/".join(m.universe_regions) for m in ms],
        "n_factors": pa.array([m.n_factors for m in ms], type=pa.int16()),
        "cov_scaling": [m.cov_scaling for m in ms],
        "specific_risk_convention": [m.specific_risk_convention for m in ms],
        "return_convention": [m.return_convention for m in ms],
        "base_model_id": [m.base_model_id for m in ms],
    }), out / "model_master.parquet", cfg)

    mids, fids, seqs, types = [], [], [], []
    for m in ms:
        ids = m.factor_ids
        mids += [m.model_id] * m.n_factors
        fids += ids
        seqs += list(range(m.n_factors))
        types += m.factor_types
    write_parquet(pa.table({
        "model_id": mids, "factor_id": fids,
        "factor_seq": pa.array(seqs, type=pa.int16()),
        "factor_name": fids, "factor_type": types,
    }), out / "factor_master.parquet", cfg)

    n = uni.n_slots
    used = uni.entry_idx < uni.n_dates
    ids = np.flatnonzero(used).astype(np.int32) + 1
    sector = stream(cfg.global_seed, "_", "sector_v2").integers(0, len(SECTORS), n)
    never = uni.exit_idx >= uni.n_dates
    end_days = days[np.clip(uni.exit_idx - 1, 0, uni.n_dates - 1)]
    write_parquet(pa.table({
        "asset_id": pa.array(ids),
        "ticker": [_ticker(int(i)) for i in ids],
        "sector": [SECTORS[sector[i - 1]] for i in ids],
        "region": [uni.region_names[uni.region[i - 1]] for i in ids],
        "country_code": pa.array(uni.country_code[used], type=pa.int16()),
        "ccy_code": pa.array(uni.ccy_code[used], type=pa.int16()),
        "start_date": pa.array(days[uni.entry_idx[used]]),
        "end_date": pa.array(end_days[used], mask=never[used]),
    }), out / "asset_master.parquet", cfg)

    write_parquet(pa.table({
        "asset_id": pa.array(np.repeat(ids, 2)),
        "vendor": ["BARRA", "AXIOMA"] * len(ids),
        "vendor_asset_id": [v for i in ids
                            for v in (f"B{int(i):07d}", f"AX{int(i):07d}")],
    }), out / "asset_xref.parquet", cfg)


class _Buf:
    """Accumulates one chunk's emissions, then builds tables."""

    def __init__(self):
        self.l = ([], [], [], [], [])      # dates, slots, seq, vals, ver
        self.c = ([], [])                  # dates_n, vals
        self.s = ([], [], [], [])          # dates, slots, vals, ver
        self.m = ([], [], [])              # dates, slots, estu
        self.r = ([], [])                  # dates_n, vals
        self.f = ([], [], [], [])          # dates, seq, slots, vals

    def add_loading(self, day, a, f, v, ver):
        n = len(v)
        self.l[0].append(np.repeat(day, n)); self.l[1].append(a)
        self.l[2].append(f); self.l[3].append(v)
        self.l[4].append(np.full(n, ver, np.int16))

    def tables(self, m: V2Model, static: ModelStatic):
        fids = m.factor_ids
        n_tri = len(static.triu[0])
        nd_c = len(self.c[0])
        cat = np.concatenate
        return {
            "factor_loading": W.loading_table(cat(self.l[0]), cat(self.l[1]),
                                              cat(self.l[2]), cat(self.l[3]),
                                              cat(self.l[4]), fids),
            "factor_covariance": W.covariance_table(
                np.repeat(np.asarray(self.c[0]), n_tri),
                np.tile(static.triu[0].astype(np.int16), nd_c),
                np.tile(static.triu[1].astype(np.int16), nd_c),
                cat(self.c[1]), fids),
            "specific_risk": W.srisk_table(cat(self.s[0]), cat(self.s[1]),
                                           cat(self.s[2]), cat(self.s[3])),
            "universe_membership": W.membership_table(cat(self.m[0]), cat(self.m[1]),
                                                      cat(self.m[2])),
            "factor_return": W.freturn_table(
                np.repeat(np.asarray(self.r[0]), m.n_factors),
                np.tile(np.arange(m.n_factors, dtype=np.int16), len(self.r[0])),
                cat(self.r[1]), fids),
            "fmp": W.fmp_table(cat(self.f[0]), cat(self.f[1]), cat(self.f[2]),
                               cat(self.f[3]), fids),
        }


def generate(cfg: V2Config, years: list[int] | None = None, quiet: bool = False,
             skip_dims: bool = False, dims_only: bool = False) -> None:
    t0 = time.perf_counter()
    days = trading_days(cfg.start_date, cfg.end_date)
    yr = years_of(days)
    all_years = sorted(set(yr.tolist()))
    sel = all_years if years is None else sorted(years)
    if sel != list(range(sel[0], sel[-1] + 1)):
        raise ValueError("years must be contiguous")

    uni = build_multi_universe(cfg, len(days))
    out = Path(cfg.output_dir)
    if not skip_dims:
        write_dimensions(cfg, uni, days, out)
    if dims_only:
        print(f"dimensions -> {out}")
        return
    if not quiet:
        print(f"calendar {len(days)} dates; slots {uni.n_slots:,}; "
              f"models {len(cfg.models)}")

    restate_rows = {"model_id": [], "cob_date": [], "version_id": [],
                    "published_date": []}

    for m in cfg.models:
        static = build_static(cfg, m, uni)
        S = uni.n_slots
        # decide chunking from a first-date estimate of loading rows/year
        live0 = uni.live_mask(0)
        n_c = int((live0 & static.covered).sum())
        nnz = m.n_styles + 1.1 + (1 if m.n_countries else 0) + (1 if m.n_currencies else 0) + 1
        monthly = n_c * nnz * 261 > cfg.monthly_chunk_threshold

        state = None
        for year in sel:
            tsel = np.flatnonzero(yr == year)
            if state is None:
                state = (fresh_state(m, static, S) if tsel[0] == 0
                         else load_checkpoint(cfg, m, year - 1))
            ty = time.perf_counter()
            months = (days[tsel].astype("datetime64[M]").astype(int) % 12
                      if monthly else np.zeros(len(tsel), int))
            rows_y = 0
            for chunk_id in sorted(set(months.tolist())):
                buf = _Buf()
                for t in tsel[months == chunk_id]:
                    t = int(t)
                    sigma, sig = advance(state, cfg, m, static, t, S)
                    live = uni.live_mask(t)
                    day = days[t]

                    a, f, v = emit_loadings(state, static, m, live)
                    buf.add_loading(day, a, f, v, 1)
                    buf.c[0].append(day); buf.c[1].append(emit_covariance(sigma, static, m))
                    si, sv = emit_specific_risk(sig, static, m, live)
                    buf.s[0].append(np.repeat(day, len(si))); buf.s[1].append(si)
                    buf.s[2].append(sv); buf.s[3].append(np.ones(len(si), np.int16))
                    mi, mf = emit_membership(static, live)
                    buf.m[0].append(np.repeat(day, len(mi))); buf.m[1].append(mi)
                    buf.m[2].append(mf)
                    buf.r[0].append(day); buf.r[1].append(emit_factor_returns(state, static, m))
                    ff, fa, fv = emit_fmp(state, static, m, live)
                    buf.f[0].append(np.repeat(day, len(fv))); buf.f[1].append(ff)
                    buf.f[2].append(fa); buf.f[3].append(fv)

                    # restatement injection (version_id = 2, published T+1..T+5)
                    g = stream(cfg.global_seed, m.model_id, "restate", t)
                    if g.random() < m.restate_rate:
                        style_rows = f < m.n_styles
                        v2 = v.copy()
                        v2[style_rows] = round_sig(
                            v[style_rows] + g.normal(0, RESTATE_STYLE_SD, int(style_rows.sum())))
                        buf.add_loading(day, a, f, v2, 2)
                        buf.s[0].append(np.repeat(day, len(si))); buf.s[1].append(si)
                        buf.s[2].append(round_sig(sv * (1 + g.normal(0, RESTATE_SRISK_SD, len(sv)))))
                        buf.s[3].append(np.full(len(si), 2, np.int16))
                        lag = 1 + int(g.integers(0, m.restate_max_lag))
                        restate_rows["model_id"].append(m.model_id)
                        restate_rows["cob_date"].append(day.astype("datetime64[D]").item())
                        restate_rows["version_id"].append(2)
                        restate_rows["published_date"].append(
                            days[min(t + lag, len(days) - 1)].astype("datetime64[D]").item())

                for name, table in buf.tables(m, static).items():
                    W.write_chunk(table, out, name, m.model_id, year, chunk_id, cfg)
                    if name == "factor_loading":
                        rows_y += table.num_rows
            save_checkpoint(cfg, m, year, state)
            if not quiet:
                print(f"{m.model_id} {year}: {rows_y:,} loading rows "
                      f"[{time.perf_counter() - ty:.1f}s]", flush=True)

    write_parquet(pa.table(restate_rows), out / "restatement_log.parquet", cfg)
    if not quiet:
        print(f"done in {(time.perf_counter() - t0) / 60:.1f} min -> {out}")
