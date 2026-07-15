"""ModelFacade: the lenient, user-facing wrapper around a strict core Model.

Adds what end users need and the core deliberately refuses: string dates and
'latest', vendor security ids, canonical units (conventions.units), wide
dataframes, discoverability, and an opt-in pre-warmable UserCache (off by
default — the staleness trade-off is the caller's). Wrap and unwrap
freely — ModelFacade(model) / facade.core — the layers stay separate.
"""

from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Sequence

import polars as pl

from conventions import (ASSET_ID, COB_DATE, FACTOR_ID, SPECIFIC_RISK,
                         T0_ESTIMATE, VALUE, scale_to_canonical,
                         sec_id_type_str)

from .cache import Coverage, NoCache, UserCache
from .core import Model
from .store import Store


class ModelFacade:

    def __init__(self, model: Model, cache: UserCache | None = None,
                 output: str = "pandas"):
        """output: dataframe library the user layer speaks — 'pandas' by
        default (the notebooks this serves lean on pandas) or 'polars'.
        Internals (core, cache) stay polars/Arrow either way; conversion
        happens once at the return boundary.

        cache: caching is OFF by default — every request goes to the store.
        Pass cache=UserCache() to opt in; the speed-vs-staleness trade-off
        is the caller's, never a default."""
        if output not in ("polars", "pandas"):
            raise ValueError(f"output must be 'polars' or 'pandas', got {output!r}")
        self._model = model
        self.cache = cache if cache is not None else NoCache()
        self.output = output
        self._frozen_latest: date | None = None   # set by from_cache only

    @classmethod
    def load(cls, model_id: str, root: str | None = None,
             output: str = "pandas",
             cache: UserCache | None = None) -> "ModelFacade":
        """One line from model name to data access.

        Args:
            model_id: Model to open, e.g. ``"AX_WW4_MH"``; validated
                against the store's model_master.
            root: Store root (local path or ``s3://``). Defaults to
                ``$FACTOR_STORE_ROOT``.
            output: Dataframe library returned to the user —
                ``"pandas"`` (default) or ``"polars"``.
            cache: Off by default. Pass ``UserCache()`` to opt in to the
                pre-warmable working-set cache.

        Returns:
            A facade over a strict core ``Model``; no caching unless
            opted in.

        Raises:
            ValueError: Unknown model, bad ``output``, or no store root.
        """
        return cls(Model(Store.open(root), model_id), cache=cache,
                   output=output)

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
            # offline sessions (from_cache) freeze 'latest' at the working
            # set's as-of date — resolving via the store would defeat the
            # point of starting without one
            if self._frozen_latest is not None:
                return self._frozen_latest
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
        xref = self._model.source.dim("asset_xref")
        if sec_id_type is not None:
            xref = xref.filter(pl.col("vendor") == sec_id_type_str(sec_id_type))
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

    def _range(self, as_of, start, end) -> tuple[date, date]:
        """Resolve the single-date vs date-range calling conventions:
        start= switches to range mode ([start, end], end defaulting to
        'latest'); otherwise [as_of, as_of]."""
        if start is not None:
            lo = self._as_date(start, "start")
            hi = self._as_date(end, "end") if end is not None \
                else self._as_date("latest")
            return lo, hi
        cob = self._as_date(as_of)
        return cob, cob

    def _loadings_long(self, lo: date, hi: date, ids, factors) -> pl.DataFrame:
        """Sparse long-form loadings over [lo, hi], via the cache."""
        if lo == hi:
            loader = lambda: self._model.factor_loadings(
                lo, assets=ids, factors=factors)
        else:
            loader = lambda: self._model.loading_history(
                lo, hi, assets=ids, factors=factors)
        long = self.cache.get("factor_loading", lo, hi, ids, loader)
        if factors is not None:                       # cache holds all factors
            long = long.filter(pl.col(FACTOR_ID).is_in(list(factors)))
        return long

    def _loadings_wide(self, long: pl.DataFrame, factors) -> pl.DataFrame:
        cols = factors if factors is not None else self.factors
        seen = set(long[FACTOR_ID].to_list())
        present = [c for c in cols if c in seen]
        wide = long.pivot(FACTOR_ID, index=[COB_DATE, ASSET_ID], values=VALUE)
        # fill only the factor columns: a frame-level fill_null(0.0) would
        # upcast the integer asset_id column to float
        return (wide.with_columns(pl.col(present).fill_null(0.0))
                .sort(COB_DATE, ASSET_ID)
                .select([COB_DATE, ASSET_ID, *present]))

    # ------------------------------------------------------------ one-liners
    def get_factor_loadings(self, as_of="latest", *, start=None, end=None,
                            assets=None, sec_id_type=None, factors=None,
                            wide: bool = True):
        """Factor loadings for one date or a date range. Loadings are
        unitless.

        Args:
            as_of: ``date``, ``datetime``, ``'YYYY-MM-DD'``, or
                ``'latest'`` (the model's last COB in the store).
            start: Range mode — loadings for every COB in [start, end]
                instead of one date. Same leniency as ``as_of``.
            end: Range end; None with ``start`` given = 'latest'.
            assets: Internal int ids, or vendor id strings resolved via
                asset_xref. None = all covered assets.
            sec_id_type: Pin the vendor scheme of ``assets``
                (a scheme string; constants on ``SecurityIDType``); omit to auto-detect.
            factors: Subset of factor ids; None = all factors.
            wide: One column per factor in factor_seq order, absent
                one-hots filled 0.0 (default). False returns sparse long
                form.

        Returns:
            Frame in the facade's ``output`` library.

        Raises:
            ValueError: Unknown security id or factor id.
        """
        lo, hi = self._range(as_of, start, end)
        ids = self._resolve_assets(assets, sec_id_type)
        long = self._loadings_long(lo, hi, ids, factors)
        if not wide:
            return self._out(long)
        return self._out(self._loadings_wide(long, factors))

    def get_security_panel(self, start="latest", end=None, securities=None, *,
                           sec_id_type=None,
                           fields=("loadings", "specific_risk", "returns")):
        """Loadings, specific risk, and asset returns joined in one panel —
        one call instead of three queries and a hand-rolled merge.

        One row per (cob_date, asset_id): the factor columns in factor_seq
        order (when 'loadings' is requested), ``specific_risk`` in canonical
        annualized decimal vol, ``return`` in canonical daily decimal. A leg
        with no data for a (date, asset) leaves nulls rather than dropping
        the row.

        Args:
            start: First COB date (leniency as elsewhere); default
                'latest' = a one-date panel.
            end: Last COB date; None = same as ``start`` (or 'latest'
                when ``start`` is a range start).
            securities: Internal int ids or vendor id strings; None = all
                covered assets.
            sec_id_type: Vendor scheme of ``securities``; omit to
                auto-detect.
            fields: Which legs to include, any subset of
                ``("loadings", "specific_risk", "returns")``.

        Returns:
            Frame in the facade's ``output`` library.

        Raises:
            ValueError: Unknown field name, security id, or empty fields.
        """
        known = ("loadings", "specific_risk", "returns")
        bad = [f for f in fields if f not in known]
        if bad or not fields:
            raise ValueError(f"fields must be a non-empty subset of {known}, "
                             f"got {tuple(fields)!r}")
        lo = self._as_date(start, "start")
        hi = self._as_date(end, "end") if end is not None else lo
        ids = self._resolve_assets(securities, sec_id_type)

        legs: list[pl.DataFrame] = []
        if "loadings" in fields:
            legs.append(self._loadings_wide(
                self._loadings_long(lo, hi, ids, None), None))
        if "specific_risk" in fields:
            srisk = self.cache.get(
                "specific_risk", lo, hi, ids,
                lambda: self._model.specific_risk_history(lo, hi, assets=ids))
            legs.append(self._scale(srisk, "specific_risk",
                                    self._model.conventions
                                    ["specific_risk_convention"])
                        .select(COB_DATE, ASSET_ID,
                                pl.col(VALUE).alias("specific_risk")))
        if "returns" in fields:
            rets = self.cache.get(
                "asset_return", lo, hi, ids,
                lambda: self._model.asset_returns(lo, hi, assets=ids))
            legs.append(self._scale(rets, "return",
                                    self._model.conventions
                                    ["return_convention"])
                        .select(COB_DATE, ASSET_ID,
                                pl.col(VALUE).alias("return")))

        panel = legs[0]
        for leg in legs[1:]:
            panel = panel.join(leg, on=[COB_DATE, ASSET_ID],
                               how="full", coalesce=True)
        return self._out(panel.sort(COB_DATE, ASSET_ID))

    def get_specific_risk(self, as_of="latest", *, assets=None,
                          sec_id_type=None):
        """Specific risk in canonical units (annualized decimal vol).

        The vendor's stored convention (e.g. ``ann_vol_pct``) is read from
        model_master and converted once at the return boundary.

        Args:
            as_of: Date leniency as in ``get_factor_loadings``.
            assets: Internal ids or vendor id strings; None = all.
            sec_id_type: Vendor scheme of ``assets``; omit to auto-detect.

        Returns:
            Frame with canonical ``value`` column, in the facade's
            ``output`` library.
        """
        cob = self._as_date(as_of)
        ids = self._resolve_assets(assets, sec_id_type)
        raw = self.cache.get(
            "specific_risk", cob, cob, ids,
            lambda: self._model.specific_risk(cob, assets=ids))
        return self._out(self._scale(
            raw, "specific_risk",
            self._model.conventions["specific_risk_convention"]))

    def get_factor_returns(self, start=None, end=None, *,
                           factors=None, estimates: bool = False):
        """Factor returns in canonical units (daily decimal).

        Args:
            start: Range start (inclusive); date leniency as elsewhere.
                None = latest date only.
            end: Range end (inclusive); None = same as ``start``.
            factors: Subset of factor ids; None = all.
            estimates: False (default) = the vendor OFFICIAL stream.
                True = the T0_ESTIMATE stream — an equality filter on the
                ``type`` column, never a join. Estimate requests bypass
                the cache, since their whole value is freshness.

        Returns:
            Frame with canonical ``value`` column.

        Raises:
            ValueError: ``estimates=True`` against a store whose
                factor_return predates the ``type`` column.
        """
        lo = self._as_date(start, "start") if start else self._as_date("latest")
        hi = self._as_date(end, "end") if end else lo
        if estimates:
            raw = self._model.factor_returns(lo, hi, factors=factors,
                                             pub_type=T0_ESTIMATE)
        else:
            raw = self.cache.get(
                "factor_return", lo, hi, None,
                lambda: self._model.factor_returns(lo, hi, factors=factors))
        if factors is not None:
            raw = raw.filter(pl.col(FACTOR_ID).is_in(list(factors)))
        return self._out(self._scale(
            raw, "return", self._model.conventions["return_convention"]))

    def get_covariance(self, as_of="latest"):
        """Factor covariance in canonical units (annualized decimal²).

        Args:
            as_of: Date leniency as elsewhere.

        Returns:
            Upper-triangle pairs (factor_id_1, factor_id_2, value).
        """
        cob = self._as_date(as_of)
        return self._out(self._scale(self._model.covariance(cob), "covariance",
                                     self._model.conventions["cov_scaling"]))

    def get_fmp_weights(self, as_of="latest", *, factors=None):
        return self._out(self._model.fmp_weights(self._as_date(as_of),
                                                 factors=factors))

    # ------------------------------------------------------------ pre-warming
    def warm(self, assets, *, as_of="latest", sec_id_type=None) -> dict:
        """Pre-warm the expected working set.

        Loads year-to-date loadings and specific risk for ``assets``, plus
        all factor returns (official stream) — the set that answers most
        day-to-day questions about a held portfolio. Subsequent covered
        requests are served from memory.

        Args:
            assets: The position list — internal ids or vendor id strings.
            as_of: Warm up to this COB date; ``'latest'`` by default.
            sec_id_type: Vendor scheme of ``assets``; omit to auto-detect.

        Returns:
            Cache stats: ``{'hits', 'misses', 'rows': {dataset: n}}``.

        Raises:
            ValueError: No cache opted in — warm() needs somewhere to
                put the working set.
        """
        if isinstance(self.cache, NoCache):
            raise ValueError(
                "caching is off by default — ModelFacade.load(..., "
                "cache=UserCache()) to opt in before warm()")
        cob = self._as_date(as_of)
        ids = self._resolve_assets(assets, sec_id_type)
        start = date(cob.year, 1, 1)
        scope = frozenset(ids)
        m = self._model
        self.cache.put("factor_loading",
                       m.loading_history(start, cob, assets=ids),
                       Coverage(start, cob, scope))
        srisk = m.source.read_fact("specific_risk", m.model_id,
                                   start=start, end=cob, assets=ids)
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
        """Persist the warmed working set for reuse across sessions.

        Keyed by (as-of date, model_id):
        ``<base>/usercache/<as_of>/<model_id>/`` — one parquet per dataset
        plus a coverage manifest. Typical pattern: ``warm(positions)`` in a
        morning job, then ``save_cache()``; later sessions ``load_cache()``
        and start hot. Same key overwrites (last warm wins).

        Args:
            path: Exact directory, bypassing the keyed layout. Default
                base is the system temp dir, overridable via
                ``$FACTOR_CACHE_DIR``.

        Returns:
            The directory written.

        Raises:
            ValueError: Nothing warmed yet.
        """
        target = (Path(path).expanduser() if path is not None else
                  self._cache_base() / self._coverage_end().isoformat()
                  / self.model_id)
        out = self.cache.to_disk(target, meta={
            "model_id": self.model_id,
            "as_of": self._coverage_end().isoformat()})
        # persist the dimension tables alongside the facts: they are what a
        # later session needs to rebuild the Model without touching the
        # store (from_cache), and they are tiny
        dims = out / "dims"
        dims.mkdir(exist_ok=True)
        for name in ("model_master", "factor_master", "asset_xref"):
            self._model.source.dim(name).write_parquet(dims / f"{name}.parquet")
        return out

    def load_cache(self, path=None, as_of=None, *,
                   max_age_days: float | None = 1.0) -> dict:
        """Adopt a working set saved by ``save_cache()``.

        Saved sets carry a TTL: the pre-warm pattern is a refresh every
        morning, so by default a set more than a day old refuses to load
        (re-warm, or pass ``max_age_days=None`` to accept it knowingly).
        Data is frozen as of its key date either way — ``clear()`` and
        re-warm on a known restatement.

        Args:
            path: Exact directory (bypasses key resolution).
            as_of: Pin a specific key date. With neither argument, the
                most recent date that has a set for this model wins.
            max_age_days: Maximum age of the saved set since it was
                written; ``None`` disables the check.

        Returns:
            Cache stats (counters start at zero for the new session).

        Raises:
            FileNotFoundError: No saved set for this model.
            ValueError: The set was saved for a different model, or is
                older than ``max_age_days``.
        """
        target = self._resolve_saved(self.model_id, path, as_of)
        cache, meta = UserCache.from_disk(target)
        saved_for = meta.get("model_id")
        if saved_for != self.model_id:
            raise ValueError(f"cached working set was saved for {saved_for!r},"
                             f" this facade is {self.model_id!r}")
        self._check_set_age(meta, max_age_days, target)
        self.cache = cache
        return self.cache.stats

    @staticmethod
    def _check_set_age(meta: dict, max_age_days: float | None,
                       target: Path) -> None:
        """TTL gate for a saved working set (skipped for sets predating the
        saved_at stamp — age unknown beats unusable)."""
        if max_age_days is None or not meta.get("saved_at"):
            return
        saved_at = datetime.fromisoformat(meta["saved_at"])
        if saved_at.tzinfo is None:            # foreign writer: assume UTC
            saved_at = saved_at.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - saved_at
        if age.total_seconds() > max_age_days * 86400:
            raise ValueError(
                f"working set at {target} is {age.days}d "
                f"{age.seconds // 3600}h old (TTL {max_age_days}d) — re-warm "
                "and save_cache(), or pass max_age_days=None to accept it")

    @classmethod
    def _resolve_saved(cls, model_id: str, path=None, as_of=None) -> Path:
        """Directory of a saved working set, by explicit path, key date, or
        (with neither) the most recent date that has a set for the model."""
        if path is not None:
            return Path(path).expanduser()
        if as_of is not None:
            if isinstance(as_of, datetime):
                as_of = as_of.date()
            d = as_of if isinstance(as_of, date) else date.fromisoformat(as_of)
            return cls._cache_base() / d.isoformat() / model_id
        base = cls._cache_base()
        dates = sorted(d.name for d in base.glob("*")
                       if (d / model_id / "manifest.json").exists())
        if not dates:
            raise FileNotFoundError(
                f"no saved working set for {model_id} under {base}")
        return base / dates[-1] / model_id   # ISO dates sort

    @classmethod
    def from_cache(cls, model_id: str, root: str | None = None, *,
                   path=None, as_of=None, output: str = "pandas",
                   max_age_days: float | None = 1.0) -> "ModelFacade":
        """Start a session entirely from a persisted working set.

        Unlike ``load()`` + ``load_cache()``, this never touches the store:
        the dimension tables come from the saved set (``save_cache()``
        persists them alongside the facts), so no store connection is
        opened — and no query box is launched — unless a later request
        falls outside the cached coverage and has to fall through.
        ``'latest'`` resolves to the set's as-of date; data is frozen as
        of that date, so re-warm when it is older than your questions.
        Like ``load_cache()``, a set older than ``max_age_days`` (default
        1 day — the morning-refresh TTL) refuses to load.

        Args:
            model_id: Model the set was saved for.
            root: Store root for fall-through requests. Defaults to
                ``$FACTOR_STORE_ROOT``; only contacted on a cache miss.
            path: Exact saved-set directory (bypasses key resolution).
            as_of: Pin a specific key date; latest available otherwise.
            output: ``"pandas"`` (default) or ``"polars"``, as in ``load()``.
            max_age_days: TTL for the saved set; ``None`` disables.

        Raises:
            FileNotFoundError: No saved set, or one without dimension
                tables (saved before dims were persisted — re-warm).
            ValueError: The set was saved for a different model, or is
                older than ``max_age_days``.
        """
        target = cls._resolve_saved(model_id, path, as_of)
        cache, meta = UserCache.from_disk(target)
        saved_for = meta.get("model_id")
        if saved_for != model_id:
            raise ValueError(f"cached working set was saved for {saved_for!r},"
                             f" requested {model_id!r}")
        cls._check_set_age(meta, max_age_days, target)
        if root is None and not os.environ.get("FACTOR_STORE_ROOT"):
            # genuinely offline machine: fine for covered sessions — only a
            # request outside the saved coverage needs a store, and this
            # placeholder makes that fall-through fail loudly when it does
            root = "no-store-configured(offline-from_cache-session)"
        dims = target / "dims"
        if not dims.is_dir():
            raise FileNotFoundError(
                f"{target} has no dims/ — the set predates offline loading; "
                "warm() and save_cache() again")
        store = Store.open(root)
        for f in dims.glob("*.parquet"):
            store._dims[f.stem] = pl.read_parquet(f)   # pre-seed: no store I/O
        fac = cls(Model(store, model_id), cache=cache, output=output)
        fac._frozen_latest = (date.fromisoformat(meta["as_of"])
                              if meta.get("as_of")
                              else max(c.end for c in cache.coverage.values()))
        return fac
