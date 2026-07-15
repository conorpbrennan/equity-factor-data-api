"""DataSource: the contract the core consumes, owned by the core layer.

Requests are semantic and model-agnostic (which fact, which dates, which
assets) — never SQL, globs, or layout. Implementations do the querying:
today Store (raw vendor units — conversion stays a user-layer convenience);
in the target state, a curated-store loader that serves canonical units,
at which point the facade's scaling step becomes a no-op it can drop.
Neither the core nor any implementation imports the facade.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, Sequence, runtime_checkable

import polars as pl


@runtime_checkable
class DataSource(Protocol):
    """What the core needs from a data backend — nothing more."""

    def dim(self, name: str) -> pl.DataFrame:
        """A dimension table by name (model_master, factor_master, ...)."""
        ...

    def read_fact(self, fact: str, model_id: str, *,
                  start: date, end: date,
                  assets: Sequence[int] | None = None,
                  factors: Sequence[str] | None = None,
                  version: int = 1,
                  pub_type: str | None = None) -> pl.DataFrame:
        """Rows of one fact for one model over [start, end], filtered.

        Returns data columns only — layout artifacts (partition columns,
        model_id) never reach the caller. None filters mean 'all'.
        """
        ...

    def has_column(self, fact: str, model_id: str, col: str) -> bool:
        """Schema probe: does this fact carry the column?"""
        ...

    def date_bounds(self, model_id: str) -> tuple[date, date]:
        """First and last COB date the model has data for."""
        ...
