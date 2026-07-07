"""Universe lifecycle (generator-spec.md §4.2).

Shared across models. Daily exit hazard annual_churn/261 over exactly n_live
live names; each exit immediately activates the next reserve slot, so the live
count is held at n_live on every date. Fully deterministic and cheap, so it is
recomputed from scratch on every run (never checkpointed).

Slot i corresponds to asset_id i+1. exit_idx is the first date index on which
the asset is NOT live; the sentinel n_dates means "live at calendar end".
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import GeneratorConfig
from .rng import stream
from .trading_calendar import BUSDAYS_PER_YEAR


@dataclass(frozen=True)
class Universe:
    entry_idx: np.ndarray   # (n_superset,) int32; sentinel n_dates = never entered
    exit_idx: np.ndarray    # (n_superset,) int32; sentinel n_dates = never exits
    n_assets: int           # slots actually used (initial cohort + activated reserves)
    n_dates: int

    def live_mask(self, t: int) -> np.ndarray:
        return (self.entry_idx <= t) & (t < self.exit_idx)


def build_universe(cfg: GeneratorConfig, n_dates: int) -> Universe:
    n_slots = cfg.n_superset
    entry = np.full(n_slots, n_dates, dtype=np.int32)
    exit_ = np.full(n_slots, n_dates, dtype=np.int32)
    alive = np.zeros(n_slots, dtype=bool)

    entry[: cfg.n_live] = 0
    alive[: cfg.n_live] = True
    cursor = cfg.n_live
    hazard = cfg.annual_churn / BUSDAYS_PER_YEAR

    for t in range(1, n_dates):
        g = stream(cfg.global_seed, "_", "universe", t)
        n_exit = int(g.binomial(cfg.n_live, hazard))
        if n_exit == 0:
            continue
        live_ids = np.flatnonzero(alive)
        leavers = g.choice(live_ids, size=n_exit, replace=False)
        alive[leavers] = False
        exit_[leavers] = t
        if cursor + n_exit > n_slots:
            raise RuntimeError(
                f"reserve pool exhausted at date index {t} "
                f"(need {cursor + n_exit} slots > n_superset={n_slots})"
            )
        entrants = np.arange(cursor, cursor + n_exit)
        entry[entrants] = t
        alive[entrants] = True
        cursor += n_exit

    return Universe(entry_idx=entry, exit_idx=exit_, n_assets=cursor, n_dates=n_dates)
