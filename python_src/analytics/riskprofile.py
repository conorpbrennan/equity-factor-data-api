"""Canonical risk-profile representation (PAS requirement, 2026-07-15).

A risk profile is an arbitrary combination of factor exposures and
specific-risk positions — a unit exposure to VALUE plus the specific risk
of one name, say. It is not representable as a basket of securities and
cannot be traded, but it is a meaningful unit of analysis: portfolios
decompose into profiles along dimensions whose parts are not portfolios.

Technically it is the object a portfolio becomes after the first step of
every analytic — computing exposures — made canonical and passable, so the
analytics can accept it directly instead of recomputing it from holdings.
Analytics functions take Portfolio | RiskProfile; a profile's exposures are
fixed (they don't drift with loadings), which is exactly its meaning.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import polars as pl

from conventions import ASSET_ID, FACTOR_ID, VALUE

from .portfolio import Portfolio

EXPOSURE = "exposure"


@dataclass(frozen=True, eq=False)
class RiskProfile:
    """Immutable: (factor_id, exposure $mm per unit loading) plus optional
    specific-risk positions (asset_id, value $mm), as of one COB date."""

    name: str
    as_of: date
    exposures: pl.DataFrame    # (factor_id Utf8, exposure Float64)
    specific: pl.DataFrame     # (asset_id Int32, value Float64); may be empty

    # ------------------------------------------------------------ construct
    @classmethod
    def from_exposures(cls, name: str, as_of: date, exposures,
                       specific=None) -> "RiskProfile":
        """Build a profile from explicit exposures.

        Args:
            name: Label carried through reporting.
            as_of: COB date the profile describes — ``datetime.date`` only.
            exposures: ``{factor_id: exposure_mm}`` mapping, or any
                frame-like with ``factor_id`` and ``exposure`` columns.
                Duplicates are summed; zero exposures are dropped.
            specific: Optional ``{asset_id: value_mm}`` (or frame-like with
                ``asset_id``/``value``) — the positions whose specific risk
                the profile carries. None = a pure factor profile.

        Raises:
            TypeError: ``as_of`` is not a ``datetime.date``.
        """
        if type(as_of) is not date:
            raise TypeError(f"as_of must be datetime.date, "
                            f"got {type(as_of).__name__!r}")
        if isinstance(exposures, dict):
            frame = pl.DataFrame({FACTOR_ID: list(exposures),
                                  EXPOSURE: [float(v)
                                             for v in exposures.values()]})
        else:
            frame = pl.DataFrame(exposures).select(FACTOR_ID, EXPOSURE)
        frame = (frame.group_by(FACTOR_ID).agg(pl.col(EXPOSURE).sum())
                 .filter(pl.col(EXPOSURE) != 0.0)
                 .with_columns(pl.col(EXPOSURE).cast(pl.Float64))
                 .sort(FACTOR_ID))
        if specific is None:
            spec = pl.DataFrame(schema={ASSET_ID: pl.Int32,
                                        VALUE: pl.Float64})
        elif isinstance(specific, dict):
            spec = pl.DataFrame(
                {ASSET_ID: list(specific),
                 VALUE: [float(v) for v in specific.values()]}
            ).with_columns(pl.col(ASSET_ID).cast(pl.Int32)).sort(ASSET_ID)
        else:
            spec = (pl.DataFrame(specific).select(ASSET_ID, VALUE)
                    .with_columns(pl.col(ASSET_ID).cast(pl.Int32),
                                  pl.col(VALUE).cast(pl.Float64))
                    .sort(ASSET_ID))
        return cls(name, as_of, frame, spec)

    @classmethod
    def from_portfolio(cls, model, portfolio: Portfolio,
                       name: str | None = None) -> "RiskProfile":
        """The canonical first step, materialized: compute the portfolio's
        exposures under ``model`` and carry its holdings as the
        specific-risk positions. The result analyzes like the portfolio
        did on its as-of date — but is now a fixed combination that can be
        sliced, negated, or combined with hand-built profiles.

        Args:
            model: Core ``Model`` or ``ModelFacade``.
            portfolio: The positions to profile.
            name: Label; defaults to ``"profile(<portfolio name>)"``.
        """
        from .functions import exposures as _exposures
        exp = _exposures(model, portfolio)
        return cls(name or f"profile({portfolio.name})", portfolio.as_of,
                   exp.select(FACTOR_ID, EXPOSURE),
                   portfolio.holdings.clone())

    # ------------------------------------------------------------ inspect
    @property
    def factors(self) -> list[str]:
        return self.exposures[FACTOR_ID].to_list()

    def __repr__(self) -> str:
        return (f"RiskProfile({self.name!r}, {self.as_of}, "
                f"{self.exposures.height} factors, "
                f"{self.specific.height} specific positions)")

    # ---------------------------------------------------------- arithmetic
    def _combine(self, other: "RiskProfile", sign: float,
                 name: str) -> "RiskProfile":
        if type(other) is not RiskProfile:
            raise TypeError(f"cannot combine RiskProfile with "
                            f"{type(other).__name__!r}")
        if self.as_of != other.as_of:
            raise ValueError(f"as_of mismatch: {self.name} is {self.as_of}, "
                             f"{other.name} is {other.as_of} — align dates "
                             "explicitly before combining")
        exp = (pl.concat([self.exposures,
                          other.exposures.with_columns(
                              pl.col(EXPOSURE) * sign)])
               .group_by(FACTOR_ID).agg(pl.col(EXPOSURE).sum()))
        spec = (pl.concat([self.specific,
                           other.specific.with_columns(
                               pl.col(VALUE) * sign)])
                .group_by(ASSET_ID).agg(pl.col(VALUE).sum()))
        return RiskProfile.from_exposures(name, self.as_of, exp, spec)

    def __add__(self, other: "RiskProfile") -> "RiskProfile":
        return self._combine(other, +1.0, f"{self.name}+{other.name}")

    def __sub__(self, other: "RiskProfile") -> "RiskProfile":
        return self._combine(other, -1.0, f"{self.name}-{other.name}")

    def __mul__(self, k: float) -> "RiskProfile":
        return RiskProfile.from_exposures(
            f"{k}*{self.name}", self.as_of,
            self.exposures.with_columns(pl.col(EXPOSURE) * float(k)),
            self.specific.with_columns(pl.col(VALUE) * float(k)))

    __rmul__ = __mul__

    def __neg__(self) -> "RiskProfile":
        return -1.0 * self
