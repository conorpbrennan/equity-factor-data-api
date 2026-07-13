"""Strict core Model: the FMA-shaped layer.

Contract: datetime.date only, internal integer asset ids only, factor ids
validated against factor_master, values returned exactly as the store holds
them (raw vendor units — conversion is a user-layer convenience). Everything
either returns a number you can rely on or raises; nothing coerces.
"""

from __future__ import annotations

from datetime import date
from typing import Sequence

import polars as pl

from conventions import (ASSET_ID, COB_DATE, FACTOR_ID, FACTOR_SEQ,
                         FACTOR_TYPE, MODEL_ID, VERSION_ID)

from .store import Store


def _require_date(value, name: str) -> date:
    # bool is not a date; datetime is excluded deliberately (a COB has no time)
    if type(value) is not date:
        raise TypeError(
            f"{name} must be datetime.date, got {type(value).__name__!r} — "
            "the core layer does not coerce; use ModelFacade for string dates")
    return value


class Model:
    """Handle on one model in one store; dimension-backed, fail-fast."""

    def __init__(self, store: Store, model_id: str):
        master = store.dim("model_master")
        row = master.filter(pl.col(MODEL_ID) == model_id)
        if row.is_empty():
            known = master[MODEL_ID].to_list()
            raise ValueError(f"unknown model {model_id!r}; store has {known}")
        self.store = store
        self.model_id = model_id
        self.meta = row.to_dicts()[0]
        self._factors = (store.dim("factor_master")
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
    def _fact(self, fact: str, where: str) -> pl.DataFrame:
        glob = self.store.fact_glob(fact, self.model_id)
        return self.store.sql(
            f"SELECT * FROM read_parquet('{glob}', hive_partitioning=true) "
            f"WHERE {where}")

    @staticmethod
    def _in(col: str, values: Sequence) -> str:
        items = ", ".join(f"'{v}'" if isinstance(v, str) else str(v)
                          for v in values)
        return f"AND {col} IN ({items}) " if values else ""

    def factor_loadings(self, as_of: date, *,
                        assets: Sequence[int] | None = None,
                        factors: Sequence[str] | None = None,
                        version: int = 1) -> pl.DataFrame:
        """Long-form loadings (sparse: nonzero rows only), raw values."""
        _require_date(as_of, "as_of")
        factors = self._check_factors(factors)
        where = (f"year = {as_of.year} AND {COB_DATE} = DATE '{as_of}' "
                 f"AND {VERSION_ID} = {version} ")
        if assets is not None:
            where += self._in(ASSET_ID, list(assets))
        if factors is not None:
            where += self._in(FACTOR_ID, factors)
        return self._fact("factor_loading", where).drop("year", MODEL_ID)

    def loading_history(self, start: date, end: date, *,
                        assets: Sequence[int],
                        factors: Sequence[str] | None = None,
                        version: int = 1) -> pl.DataFrame:
        _require_date(start, "start"), _require_date(end, "end")
        factors = self._check_factors(factors)
        where = (f"year BETWEEN {start.year} AND {end.year} "
                 f"AND {COB_DATE} BETWEEN DATE '{start}' AND DATE '{end}' "
                 f"AND {VERSION_ID} = {version} "
                 + self._in(ASSET_ID, list(assets)))
        if factors is not None:
            where += self._in(FACTOR_ID, factors)
        return (self._fact("factor_loading", where)
                .drop("year", MODEL_ID).sort(ASSET_ID, COB_DATE))

    def specific_risk(self, as_of: date, *,
                      assets: Sequence[int] | None = None,
                      version: int = 1) -> pl.DataFrame:
        _require_date(as_of, "as_of")
        where = (f"year = {as_of.year} AND {COB_DATE} = DATE '{as_of}' "
                 f"AND {VERSION_ID} = {version} ")
        if assets is not None:
            where += self._in(ASSET_ID, list(assets))
        return self._fact("specific_risk", where).drop("year", MODEL_ID)

    def factor_returns(self, start: date, end: date, *,
                       factors: Sequence[str] | None = None,
                       version: int = 1) -> pl.DataFrame:
        _require_date(start, "start"), _require_date(end, "end")
        factors = self._check_factors(factors)
        where = (f"year BETWEEN {start.year} AND {end.year} "
                 f"AND {COB_DATE} BETWEEN DATE '{start}' AND DATE '{end}' "
                 f"AND {VERSION_ID} = {version} ")
        if factors is not None:
            where += self._in(FACTOR_ID, factors)
        return (self._fact("factor_return", where)
                .drop("year", MODEL_ID).sort(FACTOR_ID, COB_DATE))

    def covariance(self, as_of: date, *, version: int = 1) -> pl.DataFrame:
        _require_date(as_of, "as_of")
        return self._fact(
            "factor_covariance",
            f"year = {as_of.year} AND {COB_DATE} = DATE '{as_of}' "
            f"AND {VERSION_ID} = {version} ").drop("year", MODEL_ID)

    def fmp_weights(self, as_of: date, *,
                    factors: Sequence[str] | None = None,
                    version: int = 1) -> pl.DataFrame:
        _require_date(as_of, "as_of")
        factors = self._check_factors(factors)
        where = (f"year = {as_of.year} AND {COB_DATE} = DATE '{as_of}' "
                 f"AND {VERSION_ID} = {version} ")
        if factors is not None:
            where += self._in(FACTOR_ID, factors)
        return self._fact("fmp", where).drop("year", MODEL_ID)

    def dates(self) -> tuple[date, date]:
        """First and last COB date this model has data for."""
        glob = self.store.fact_glob("specific_risk", self.model_id)
        row = self.store.sql(f"SELECT min({COB_DATE}) lo, max({COB_DATE}) hi "
                             f"FROM read_parquet('{glob}')").to_dicts()[0]
        return row["lo"], row["hi"]
