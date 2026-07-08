"""Multi-region universes (spec §1): one churned universe per region, mapped
into a single global slot space. asset_id = global_slot + 1."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from generator.rng import stream
from generator.trading_calendar import BUSDAYS_PER_YEAR

from .fleet import (N_CCY_CODES, N_COUNTRY_CODES, REGION_CCY_RANGE,
                    REGION_COUNTRY_RANGE, REGIONS, SUPERSET_FACTOR, V2Config)

ANNUAL_CHURN = 0.05


@dataclass(frozen=True)
class MultiUniverse:
    entry_idx: np.ndarray      # (S,) int32; sentinel n_dates
    exit_idx: np.ndarray       # (S,) int32; sentinel n_dates
    region: np.ndarray         # (S,) small int, index into region_names
    region_names: tuple[str, ...]
    region_offsets: dict[str, tuple[int, int]]   # region -> (start, end) slot range
    country_code: np.ndarray   # (S,) int16, universal code space
    ccy_code: np.ndarray       # (S,) int16
    n_slots: int
    n_dates: int

    def live_mask(self, t: int) -> np.ndarray:
        return (self.entry_idx <= t) & (t < self.exit_idx)

    def region_mask(self, regions: tuple[str, ...]) -> np.ndarray:
        wanted = [i for i, r in enumerate(self.region_names) if r in regions]
        return np.isin(self.region, wanted)


def _churn_region(cfg: V2Config, region: str, n_live: int, n_super: int,
                  n_dates: int) -> tuple[np.ndarray, np.ndarray]:
    entry = np.full(n_super, n_dates, dtype=np.int32)
    exit_ = np.full(n_super, n_dates, dtype=np.int32)
    alive = np.zeros(n_super, dtype=bool)
    entry[:n_live] = 0
    alive[:n_live] = True
    cursor = n_live
    hazard = ANNUAL_CHURN / BUSDAYS_PER_YEAR
    for t in range(1, n_dates):
        g = stream(cfg.global_seed, "_", f"universe_{region}", t)
        n_exit = int(g.binomial(n_live, hazard))
        if n_exit == 0:
            continue
        leavers = g.choice(np.flatnonzero(alive), size=n_exit, replace=False)
        alive[leavers] = False
        exit_[leavers] = t
        if cursor + n_exit > n_super:
            raise RuntimeError(f"{region}: reserve pool exhausted at t={t}")
        entry[cursor:cursor + n_exit] = t
        alive[cursor:cursor + n_exit] = True
        cursor += n_exit
    return entry, exit_


def build_multi_universe(cfg: V2Config, n_dates: int) -> MultiUniverse:
    names = tuple(REGIONS)
    supers = {r: int(REGIONS[r] * SUPERSET_FACTOR) for r in names}
    total = sum(supers.values())
    entry = np.empty(total, dtype=np.int32)
    exit_ = np.empty(total, dtype=np.int32)
    region = np.empty(total, dtype=np.int8)
    country = np.empty(total, dtype=np.int16)
    ccy = np.empty(total, dtype=np.int16)
    offsets: dict[str, tuple[int, int]] = {}

    pos = 0
    for ri, r in enumerate(names):
        n_super = supers[r]
        e, x = _churn_region(cfg, r, REGIONS[r], n_super, n_dates)
        sl = slice(pos, pos + n_super)
        entry[sl], exit_[sl] = e, x
        region[sl] = ri
        g = stream(cfg.global_seed, "_", f"geo_{r}")
        clo, chi = REGION_COUNTRY_RANGE[r]
        country[sl] = g.integers(clo, chi, n_super).astype(np.int16)
        ylo, yhi = REGION_CCY_RANGE[r]
        ccy[sl] = g.integers(ylo, yhi, n_super).astype(np.int16)
        offsets[r] = (pos, pos + n_super)
        pos += n_super

    assert country.max() < N_COUNTRY_CODES and ccy.max() < N_CCY_CODES
    return MultiUniverse(entry_idx=entry, exit_idx=exit_, region=region,
                         region_names=names, region_offsets=offsets,
                         country_code=country, ccy_code=ccy,
                         n_slots=total, n_dates=n_dates)
