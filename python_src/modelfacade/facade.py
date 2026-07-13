"""ModelFacade: the lenient, user-facing wrapper around a strict core Model.

Adds what end users need and the core deliberately refuses: string dates and
'latest', vendor security ids, canonical units (conventions.units), wide
dataframes, discoverability, and the pre-warmable UserCache. Wrap and unwrap
freely — ModelFacade(model) / facade.core — the layers stay separate.
"""

from __future__ import annotations

import os
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Sequence

import polars as pl

from conventions import (ASSET_ID, COB_DATE, FACTOR_ID, SPECIFIC_RISK, VALUE,
                         SecurityIDType, scale_to_canonical)

from .cache import Coverage, UserCache
from .core import Model
from .store import Store


class ModelFacade:

    def __init__(self, model: Model, cache: UserCache | None = None,
                 output: str = "polars"):
        """output: dataframe library the user layer speaks — 'polars' or
        'pandas'. Internals (core, cache) stay polars/Arrow either way;
        conversion happens once at the return boundary."""
        if output not in ("polars", "pandas"):
            raise ValueError(f"output must be 'polars' or 'pandas', got {output!r}")
        self._model = model
        self.cache = cache or UserCache()
        self.output = output

    @classmethod
    def load(cls, model_id: str, root: str | None = None,
             output: str = "polars") -> "ModelFacade":
        """One line from model name to data access ($FACTOR_STORE_ROOT)."""
        return cls(Model(Store.open(root), model_id), output=output)

    def _out(self, frame: pl.DataFrame):
        """Convert at the return boundary, per the facade's output setting."""
        if self.output == "pandas":
            try:
                return frame.to_pandas()
            except ModuleNotFoundError as e:
                raise ModuleNotFoundError(
                    "output='pandas' needs pandas installed — pip install "
                    "pandas (facade internals run on polars either way)") from e
        return frame

    # -------------------------------------------------------- discoverability
    @property
    def core(self) -> Model:
        """The strict layer back, for handing into core computations."""
        return self._model

    @property
    def model_id(self) -> str:
        return self._model.model_id

    @property
    def factors(self) -> list[str]:
        return self._model.factors

    @property
    def styles(self) -> list[str]:
        types = self._model.factor_types
        return [f for f in self._model.factors if types[f] == "STYLE"]

    def describe(self) -> dict:
        lo, hi = self._model.dates()
        meta = self._model.meta
        return {
            "model_id": self.model_id, "vendor": meta["vendor"],
            "region": meta["region"], "n_factors": meta["n_factors"],
            "styles": self.styles, "first_date": lo, "last_date": hi,
            "raw_units": self._model.conventions,
            "base_model_id": meta["base_model_id"],
        }

    # --------------------------------------------------------------- leniency
    def _as_date(self, value, name: str = "as_of") -> date:
        if value in (None, "latest"):
            return self._model.dates()[1]
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            return date.fromisoformat(value)
        raise TypeError(f"{name}: expected date, 'YYYY-MM-DD' or 'latest', "
                        f"got {type(value).__name__!r}")

    def _resolve_assets(self, assets, sec_id_type=None) -> list[int] | None:
        """Internal ints pass through; vendor id strings resolve via asset_xref."""
        if assets is None:
            return None
        assets = list(assets)
        if all(isinstance(a, int) for a in assets):
            return assets
        xref = self._model.store.dim("asset_xref")
        if sec_id_type is not None:
            xref = xref.filter(pl.col("vendor") == str(SecurityIDType(sec_id_type)))
        wanted = [str(a) for a in assets]
        hits = xref.filter(pl.col("vendor_asset_id").is_in(wanted))
        missing = set(wanted) - set(hits["vendor_asset_id"])
        if missing:
            raise ValueError(f"unknown security ids: {sorted(missing)} "
                             "(pass sec_id_type= to disambiguate the scheme)")
        return hits[ASSET_ID].unique().to_list()

    def _scale(self, frame: pl.DataFrame, kind: str, convention: str,
               col: str = VALUE) -> pl.DataFrame:
        return frame.with_columns(pl.col(col) * scale_to_canonical(kind, convention))

    # ------------------------------------------------------------ one-liners
    def get_factor_loadings(self, as_of="latest", *, assets=None,
                            sec_id_type=None, factors=None,
                            wide: bool = True):
        """Loadings for one date; wide (one column per factor, absent = 0.0)
        or long. Loadings are unitless — no conversion applies."""
        cob = self._as_date(as_of)
        ids = self._resolve_assets(assets, sec_id_type)
        long = self.cache.get(
            "factor_loading", cob, cob, ids,
            lambda: self._model.factor_loadings(cob, assets=ids, factors=factors))
        if factors is not None:                       # cache holds all factors
            long = long.filter(pl.col(FACTOR_ID).is_in(list(factors)))
        if not wide:
            return self._out(long)
        cols = factors if factors is not None else self.factors
        seen = set(long[FACTOR_ID].to_list())
        present = [c for c in cols if c in seen]
        wide = long.pivot(FACTOR_ID, index=[COB_DATE, ASSET_ID], values=VALUE)
        # fill only the factor columns: a frame-level fill_null(0.0) would
        # upcast the integer asset_id column to float
        return self._out(wide.with_columns(pl.col(present).fill_null(0.0))
                         .sort(ASSET_ID)
                         .select([COB_DATE, ASSET_ID, *present]))

    def get_specific_risk(self, as_of="latest", *, assets=None,
                          sec_id_type=None):
        """Annualized decimal vol (canonical), whatever the vendor stored."""
        cob = self._as_date(as_of)
        ids = self._resolve_assets(assets, sec_id_type)
        raw = self.cache.get(
            "specific_risk", cob, cob, ids,
            lambda: self._model.specific_risk(cob, assets=ids))
        return self._out(self._scale(
            raw, "specific_risk",
            self._model.conventions["specific_risk_convention"]))

    def get_factor_returns(self, start=None, end=None, *,
                           factors=None):
        """Daily decimal returns (canonical). Defaults to the latest date."""
        lo = self._as_date(start, "start") if start else self._as_date("latest")
        hi = self._as_date(end, "end") if end else lo
        raw = self.cache.get(
            "factor_return", lo, hi, None,
            lambda: self._model.factor_returns(lo, hi, factors=factors))
        if factors is not None:
            raw = raw.filter(pl.col(FACTOR_ID).is_in(list(factors)))
        return self._out(self._scale(
            raw, "return", self._model.conventions["return_convention"]))

    def get_covariance(self, as_of="latest"):
        """Annualized decimal^2 factor covariance (canonical)."""
        cob = self._as_date(as_of)
        return self._out(self._scale(self._model.covariance(cob), "covariance",
                                     self._model.conventions["cov_scaling"]))

    def get_fmp_weights(self, as_of="latest", *, factors=None):
        return self._out(self._model.fmp_weights(self._as_date(as_of),
                                                 factors=factors))

    # ------------------------------------------------------------ pre-warming
    def warm(self, assets, *, as_of="latest", sec_id_type=None) -> dict:
        """Pre-warm the expected working set: year-to-date loadings and
        specific risk for `assets`, plus all factor returns — the set that
        answers most day-to-day questions about a held portfolio."""
        cob = self._as_date(as_of)
        ids = self._resolve_assets(assets, sec_id_type)
        start = date(cob.year, 1, 1)
        scope = frozenset(ids)
        m = self._model
        self.cache.put("factor_loading",
                       m.loading_history(start, cob, assets=ids),
                       Coverage(start, cob, scope))
        srisk = m.store.sql(
            f"SELECT * FROM read_parquet("
            f"'{m.store.fact_glob('specific_risk', m.model_id)}', "
            f"hive_partitioning=true) "
            f"WHERE year = {cob.year} "
            f"AND {COB_DATE} BETWEEN DATE '{start}' AND DATE '{cob}' "
            f"AND {ASSET_ID} IN ({', '.join(map(str, ids))}) "
            f"AND version_id = 1").drop("year", "model_id")
        self.cache.put("specific_risk", srisk, Coverage(start, cob, scope))
        self.cache.put("factor_return",
                       m.factor_returns(start, cob),
                       Coverage(start, cob, None))
        return self.cache.stats

    # ------------------------------------------------- cache persistence
    # Layout: <base>/usercache/<as_of>/<model_id>/<dataset>.parquet — the
    # key is (as-of COB date, model_id); the date names what the data IS
    # (coverage end of the warm), not when it was saved. Base defaults to
    # the system temp dir, so working sets self-expire with it;
    # $FACTOR_CACHE_DIR overrides for a persistent location.
    @staticmethod
    def _cache_base() -> Path:
        base = os.environ.get("FACTOR_CACHE_DIR", tempfile.gettempdir())
        return Path(base).expanduser() / "usercache"

    def _coverage_end(self) -> date:
        ends = [c.end for c in self.cache.coverage.values()]
        if not ends:
            raise ValueError("nothing warmed — call warm() before save_cache()")
        return max(ends)

    def save_cache(self, path=None) -> Path:
        """Persist the warmed working set for reuse across sessions, keyed
        by (as-of date, model_id): <base>/usercache/<as_of>/<model_id>/.
        Typical pattern: warm(positions) in a morning job, save_cache();
        later sessions load_cache() and start hot. Same key overwrites
        (last warm wins). Explicit path= bypasses the keyed layout."""
        target = (Path(path).expanduser() if path is not None else
                  self._cache_base() / self._coverage_end().isoformat()
                  / self.model_id)
        return self.cache.to_disk(target, meta={"model_id": self.model_id})

    def load_cache(self, path=None, as_of=None) -> dict:
        """Adopt a saved working set; refuses one saved for a different
        model. No arguments: the most recent as-of date that has a set for
        this model. as_of= pins a specific date; path= an exact directory.
        Data is frozen as of its key date — re-warm when it's older than
        the questions you're asking. Returns cache stats."""
        if path is not None:
            target = Path(path).expanduser()
        elif as_of is not None:
            target = (self._cache_base() / self._as_date(as_of).isoformat()
                      / self.model_id)
        else:
            base = self._cache_base()
            dates = sorted(d.name for d in base.glob("*")
                           if (d / self.model_id / "manifest.json").exists())
            if not dates:
                raise FileNotFoundError(
                    f"no saved working set for {self.model_id} under {base}")
            target = base / dates[-1] / self.model_id   # ISO dates sort
        cache, meta = UserCache.from_disk(target)
        saved_for = meta.get("model_id")
        if saved_for != self.model_id:
            raise ValueError(f"cached working set was saved for {saved_for!r},"
                             f" this facade is {self.model_id!r}")
        self.cache = cache
        return self.cache.stats
