"""Strict core Model: the systems-facing layer.

Contract: datetime.date only, internal integer asset ids only, factor ids
validated against factor_master, values returned exactly as the source holds
them (raw vendor units — conversion is a user-layer convenience). Everything
either returns a number you can rely on or raises; nothing coerces.

Data access goes through the DataSource protocol (datasource.py): the core
owns the contract but no implementation — it composes no SQL and knows no
storage layout. Store satisfies the protocol today; a curated model store
can replace it without the core changing.
"""

from __future__ import annotations

from datetime import date
from typing import Sequence

import polars as pl

from conventions import (ASSET_ID, COB_DATE, FACTOR_ID, FACTOR_SEQ,
                         FACTOR_TYPE, MODEL_ID, OFFICIAL, PUB_TYPES, TYPE)

from .datasource import DataSource


def _require_date(value, name: str) -> date:
    # bool is not a date; datetime is excluded deliberately (a COB has no time)
    if type(value) is not date:
        raise TypeError(
            f"{name} must be datetime.date, got {type(value).__name__!r} — "
            "the core layer does not coerce; use ModelFacade for string dates")
    return value


class Model:
    """Handle on one model in one data source; dimension-backed, fail-fast."""

    def __init__(self, source: DataSource, model_id: str):
        master = source.dim("model_master")
        row = master.filter(pl.col(MODEL_ID) == model_id)
        if row.is_empty():
            known = master[MODEL_ID].to_list()
            raise ValueError(f"unknown model {model_id!r}; store has {known}")
        self.source = source
        self.model_id = model_id
        self.meta = row.to_dicts()[0]
        self._factors = (source.dim("factor_master")
                         .filter(pl.col(MODEL_ID) == model_id)
                         .sort(FACTOR_SEQ))

    # ------------------------------------------------------------- metadata
    @property
    def factors(self) -> list[str]:
        return self._factors[FACTOR_ID].to_list()

    @property
    def factor_types(self) -> dict[str, str]:
        return dict(zip(self._factors[FACTOR_ID], self._factors[FACTOR_TYPE]))

    @property
    def conventions(self) -> dict[str, str]:
        """Vendor unit conventions this model's raw values are stored in."""
        return {k: self.meta[k] for k in
                ("cov_scaling", "specific_risk_convention", "return_convention")}

    def _check_factors(self, factors: Sequence[str] | None) -> list[str] | None:
        if factors is None:
            return None
        bad = sorted(set(factors) - set(self.factors))
        if bad:
            raise ValueError(f"not factors of {self.model_id}: {bad}")
        return list(factors)

    # ----------------------------------------------------------------- facts
    def factor_loadings(self, as_of: date, *,
                        assets: Sequence[int] | None = None,
                        factors: Sequence[str] | None = None,
                        version: int = 1) -> pl.DataFrame:
        """Long-form loadings for one date (sparse: nonzero rows only).

        Args:
            as_of: COB date — ``datetime.date`` only; never coerced.
            assets: Internal integer asset ids; None = all covered.
            factors: Factor ids, validated against factor_master.
            version: Publication version; 1 = original, >1 = restatement.

        Returns:
            Columns ``cob_date, asset_id, factor_id, value, version_id``;
            values raw, exactly as the vendor published.

        Raises:
            TypeError: ``as_of`` is not a ``datetime.date``.
            ValueError: Unknown factor id.
        """
        _require_date(as_of, "as_of")
        factors = self._check_factors(factors)
        return self.source.read_fact(
            "factor_loading", self.model_id, start=as_of, end=as_of,
            assets=assets, factors=factors, version=version)

    def loading_history(self, start: date, end: date, *,
                        assets: Sequence[int],
                        factors: Sequence[str] | None = None,
                        version: int = 1) -> pl.DataFrame:
        _require_date(start, "start"), _require_date(end, "end")
        factors = self._check_factors(factors)
        return (self.source.read_fact(
                    "factor_loading", self.model_id, start=start, end=end,
                    assets=assets, factors=factors, version=version)
                .sort(ASSET_ID, COB_DATE))

    def asset_returns(self, start: date, end: date, *,
                      assets: Sequence[int] | None = None,
                      version: int = 1) -> pl.DataFrame:
        """Per-asset total returns, raw vendor units (same convention as
        factor returns per model_master). The input to T0 estimation:
        FMP weights × asset returns. Raises like every core read; a store
        without the asset_return dataset simply has no rows to serve."""
        _require_date(start, "start"), _require_date(end, "end")
        return (self.source.read_fact(
                    "asset_return", self.model_id, start=start, end=end,
                    assets=assets, version=version)
                .sort(ASSET_ID, COB_DATE))

    def specific_risk(self, as_of: date, *,
                      assets: Sequence[int] | None = None,
                      version: int = 1) -> pl.DataFrame:
        _require_date(as_of, "as_of")
        return self.source.read_fact(
            "specific_risk", self.model_id, start=as_of, end=as_of,
            assets=assets, version=version)

    def factor_returns(self, start: date, end: date, *,
                       factors: Sequence[str] | None = None,
                       version: int = 1,
                       pub_type: str = OFFICIAL) -> pl.DataFrame:
        """Factor returns for one publication stream, raw vendor units.

        Args:
            start: Range start (inclusive), ``datetime.date`` only.
            end: Range end (inclusive), ``datetime.date`` only.
            factors: Factor ids, validated; None = all.
            version: Publication version; 1 = original, >1 = restatement.
            pub_type: Which stream — ``OFFICIAL`` or ``T0_ESTIMATE`` — via
                an equality filter on the ``type`` column; orthogonal to
                ``version``. A store predating the column serves OFFICIAL
                (its only stream) and refuses estimates.

        Raises:
            TypeError: Non-date arguments.
            ValueError: Unknown ``pub_type``/factor, or estimates
                requested from a store without the ``type`` column.
        """
        _require_date(start, "start"), _require_date(end, "end")
        if pub_type not in PUB_TYPES:
            raise ValueError(f"pub_type must be one of {PUB_TYPES}, "
                             f"got {pub_type!r}")
        factors = self._check_factors(factors)
        stream: str | None = pub_type
        if not self.source.has_column("factor_return", self.model_id, TYPE):
            if pub_type != OFFICIAL:
                raise ValueError(
                    f"{self.model_id}: factor_return has no {TYPE!r} column — "
                    "this store carries no estimate stream")
            stream = None                      # pre-type store: no filter
        return (self.source.read_fact(
                    "factor_return", self.model_id, start=start, end=end,
                    factors=factors, version=version, pub_type=stream)
                .sort(FACTOR_ID, COB_DATE))

    def covariance(self, as_of: date, *, version: int = 1) -> pl.DataFrame:
        _require_date(as_of, "as_of")
        return self.source.read_fact(
            "factor_covariance", self.model_id, start=as_of, end=as_of,
            version=version)

    def fmp_weights(self, as_of: date, *,
                    factors: Sequence[str] | None = None,
                    version: int = 1) -> pl.DataFrame:
        _require_date(as_of, "as_of")
        factors = self._check_factors(factors)
        return self.source.read_fact(
            "fmp", self.model_id, start=as_of, end=as_of,
            factors=factors, version=version)

    def dates(self) -> tuple[date, date]:
        """First and last COB date this model has data for."""
        return self.source.date_bounds(self.model_id)
