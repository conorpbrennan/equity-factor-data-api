"""Canonical portfolio representation.

One class whether the holdings are booked positions, a benchmark, or a
hypothetical basket — so analytics take *a portfolio*, not a dataframe with
folklore about its column names. Holdings are internal integer asset ids and
market values in millions USD (conventions units); weights are derived on
demand, never stored. Instances are immutable; arithmetic returns new
portfolios aligned on asset_id, so `positions - benchmark` is the active
portfolio, ready for the same analytics as its parents.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import polars as pl

from conventions import ASSET_ID, VALUE


@dataclass(frozen=True, eq=False)
class Portfolio:
    """Immutable holdings snapshot: (asset_id, value $mm) as of one COB date."""

    name: str
    as_of: date
    holdings: pl.DataFrame     # (asset_id Int32, value Float64), aggregated

    # ------------------------------------------------------------ construct
    @classmethod
    def from_holdings(cls, name: str, as_of: date, holdings) -> "Portfolio":
        """Build a portfolio from holdings.

        Args:
            name: Label carried through arithmetic and reporting.
            as_of: COB date of the snapshot — ``datetime.date`` only.
            holdings: ``{asset_id: value_mm}`` mapping, or any frame-like
                with ``asset_id`` and ``value`` columns. Duplicate asset
                rows are summed; zero-value holdings are dropped.

        Returns:
            A normalized, immutable Portfolio.

        Raises:
            TypeError: ``as_of`` is not a ``datetime.date``.
        """
        if type(as_of) is not date:
            raise TypeError(f"as_of must be datetime.date, "
                            f"got {type(as_of).__name__!r}")
        if isinstance(holdings, dict):
            frame = pl.DataFrame({ASSET_ID: list(holdings),
                                  VALUE: [float(v) for v in holdings.values()]})
        else:
            frame = pl.DataFrame(holdings).select(ASSET_ID, VALUE)
        frame = (frame.group_by(ASSET_ID).agg(pl.col(VALUE).sum())
                 .filter(pl.col(VALUE) != 0.0)
                 .with_columns(pl.col(ASSET_ID).cast(pl.Int32),
                               pl.col(VALUE).cast(pl.Float64))
                 .sort(ASSET_ID))
        return cls(name, as_of, frame)

    # ------------------------------------------------------------ inspect
    @property
    def assets(self) -> list[int]:
        return self.holdings[ASSET_ID].to_list()

    @property
    def gross(self) -> float:
        """Gross market value, $mm (sum of absolute holdings)."""
        return float(self.holdings[VALUE].abs().sum())

    @property
    def net(self) -> float:
        """Net market value, $mm."""
        return float(self.holdings[VALUE].sum())

    def weights(self) -> pl.DataFrame:
        """(asset_id, weight) with weight = value / gross — derived, never
        stored, so values stay the single source of truth."""
        return self.holdings.with_columns(
            (pl.col(VALUE) / self.gross).alias("weight")).drop(VALUE)

    def __repr__(self) -> str:
        return (f"Portfolio({self.name!r}, {self.as_of}, "
                f"{self.holdings.height} assets, net {self.net:.1f}mm)")

    # ---------------------------------------------------------- arithmetic
    def _combine(self, other: "Portfolio", sign: float,
                 name: str) -> "Portfolio":
        if type(other) is not Portfolio:
            raise TypeError(f"cannot combine Portfolio with "
                            f"{type(other).__name__!r}")
        if self.as_of != other.as_of:
            raise ValueError(f"as_of mismatch: {self.name} is {self.as_of}, "
                             f"{other.name} is {other.as_of} — align dates "
                             "explicitly before combining")
        merged = (self.holdings.join(other.holdings, on=ASSET_ID,
                                     how="full", coalesce=True, suffix="_o")
                  .fill_null(0.0)
                  .with_columns((pl.col(VALUE)
                                 + sign * pl.col(f"{VALUE}_o")).alias(VALUE))
                  .select(ASSET_ID, VALUE))
        return Portfolio.from_holdings(name, self.as_of, merged)

    def __add__(self, other: "Portfolio") -> "Portfolio":
        return self._combine(other, +1.0, f"{self.name}+{other.name}")

    def __sub__(self, other: "Portfolio") -> "Portfolio":
        """positions - benchmark = the active portfolio."""
        return self._combine(other, -1.0, f"{self.name}-{other.name}")

    def __mul__(self, k: float) -> "Portfolio":
        scaled = self.holdings.with_columns(pl.col(VALUE) * float(k))
        return Portfolio.from_holdings(f"{k}*{self.name}", self.as_of, scaled)

    __rmul__ = __mul__

    def __neg__(self) -> "Portfolio":
        return -1.0 * self
