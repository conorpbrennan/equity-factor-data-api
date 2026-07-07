"""Dimension tables (generator-spec.md §5): model/factor/asset masters, xref."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pyarrow as pa

from .config import SECTORS, GeneratorConfig
from .rng import stream
from .universe import Universe
from .writer import write_parquet

_BASE36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _ticker(asset_id: int) -> str:
    n = asset_id - 1
    chars = []
    for _ in range(3):
        chars.append(chr(ord("A") + n % 26))
        n //= 26
    return "".join(reversed(chars))


def _base36(n: int) -> str:
    s = ""
    while n:
        s = _BASE36[n % 36] + s
        n //= 36
    return s.rjust(5, "0")


def write_dimensions(cfg: GeneratorConfig, uni: Universe, days: np.ndarray, out: Path) -> None:
    # model_master
    write_parquet(pa.table({
        "model_id": [m.model_id for m in cfg.models],
        "vendor": [m.vendor for m in cfg.models],
        "model_name": [m.model_name for m in cfg.models],
        "variant": [m.variant for m in cfg.models],
        "region": ["US"] * len(cfg.models),
        "n_factors": pa.array([m.n_factors for m in cfg.models], type=pa.int16()),
        "cov_scaling": [m.cov_scaling for m in cfg.models],
        "specific_risk_convention": [m.specific_risk_convention for m in cfg.models],
    }), out / "model_master.parquet", cfg)

    # factor_master
    mids, fids, seqs, names, types = [], [], [], [], []
    for m in cfg.models:
        ids = m.factor_ids
        mids += [m.model_id] * m.n_factors
        fids += ids
        seqs += list(range(m.n_factors))
        names += ids  # synthetic: mnemonic doubles as name
        types += m.factor_types
    write_parquet(pa.table({
        "model_id": mids,
        "factor_id": fids,
        "factor_seq": pa.array(seqs, type=pa.int16()),
        "factor_name": names,
        "factor_type": types,
    }), out / "factor_master.parquet", cfg)

    # asset_master — slots that ever existed
    n = uni.n_assets
    ids = np.arange(1, n + 1, dtype=np.int32)
    sector_idx = stream(cfg.global_seed, "_", "sector").integers(0, len(SECTORS), cfg.n_superset)[:n]
    entry = uni.entry_idx[:n]
    exit_ = uni.exit_idx[:n]
    never_exits = exit_ >= uni.n_dates
    end_days = days[np.clip(exit_ - 1, 0, uni.n_dates - 1)]
    write_parquet(pa.table({
        "asset_id": pa.array(ids),
        "ticker": [_ticker(i) for i in ids],
        "sector": [SECTORS[j] for j in sector_idx],
        "country": ["US"] * n,
        "start_date": pa.array(days[entry]),
        "end_date": pa.array(end_days, mask=never_exits),
    }), out / "asset_master.parquet", cfg)

    # asset_xref — one Barra ID and one Axioma ID per asset
    write_parquet(pa.table({
        "asset_id": pa.array(np.repeat(ids, 2)),
        "vendor": ["BARRA", "AXIOMA"] * n,
        "vendor_asset_id": [
            vid for i in ids for vid in (f"USA{_base36(int(i))}", f"AX{int(i):07d}")
        ],
    }), out / "asset_xref.parquet", cfg)
