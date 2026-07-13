"""The two fundamental analytics as stateless functions.

Shape: (model, portfolio, ...) -> DataFrame. No hidden state, no mutation —
the model supplies data, the portfolio supplies scope, the function
computes. Both accept the strict core Model or a ModelFacade (unwrapped via
.core), and read raw store values, converting units themselves through the
conventions library — so they compose with either layer without double
conversion.

Units: holdings are $mm (conventions money), loadings unitless, returns
canonical daily decimal — so exposures are "$mm per unit loading" and PnL
contributions are $mm.
"""

from __future__ import annotations

from datetime import date

import polars as pl

from conventions import (ASSET_ID, COB_DATE, FACTOR_ID, OFFICIAL,
                         T0_ESTIMATE, VALUE, WEIGHT, scale_to_canonical)

from .portfolio import Portfolio

_POS = "position_value"      # holdings value, renamed to avoid clashing with
                             # the loadings' raw `value` column


def _core(model):
    """Accept a core Model or a ModelFacade; analytics run on the core."""
    return model.core if hasattr(model, "core") else model


def _ordered(frame: pl.DataFrame, factors: list[str]) -> pl.DataFrame:
    """Sort factor rows into the model's factor_seq order."""
    order = {f: i for i, f in enumerate(factors)}
    return (frame.with_columns(pl.col(FACTOR_ID).replace_strict(order)
                               .alias("_seq"))
            .sort("_seq").drop("_seq"))


def exposures(model, portfolio: Portfolio, *,
              factors: list[str] | None = None) -> pl.DataFrame:
    """Value-weighted factor exposures at the portfolio's as-of date.

    exposure_f = Σ_i value_i · loading_{i,f} — in $mm per unit loading.
    Factors no holding loads on are absent (a sparse store row that doesn't
    exist is an exposure of zero).

    Args:
        model: Core ``Model`` or ``ModelFacade``.
        portfolio: Scope and as-of date; holdings in $mm.
        factors: Subset of factor ids; None = all model factors.

    Returns:
        (factor_id, exposure) in factor_seq order.
    """
    core = _core(model)
    loadings = core.factor_loadings(portfolio.as_of,
                                    assets=portfolio.assets, factors=factors)
    holdings = portfolio.holdings.rename({VALUE: _POS})
    out = (loadings.join(holdings, on=ASSET_ID)
           .group_by(FACTOR_ID)
           .agg((pl.col(VALUE) * pl.col(_POS)).sum().alias("exposure")))
    return _ordered(out, core.factors)


def pnl_decomposition(model, portfolio: Portfolio, *,
                      start: date, end: date | None = None,
                      estimates: bool = False) -> pl.DataFrame:
    """Per-factor PnL contributions over a date range.

    contribution_{f,d} = exposure_{f,d} · return_{f,d} — exposures
    recomputed from each date's loadings, returns converted to canonical
    daily decimal, so contributions are $mm. Holdings are held constant
    over the window (buy-and-hold values; no drift, no trades) — the
    scaffold simplification to state up front.

    Args:
        model: Core ``Model`` or ``ModelFacade``.
        portfolio: Scope; holdings in $mm.
        start: Range start (inclusive), ``datetime.date``.
        end: Range end (inclusive); None = ``start`` only.
        estimates: False = the vendor OFFICIAL return stream. True = the
            T0_ESTIMATE stream — the same decomposition becomes the flash
            PnL, available at the end of the day it describes.

    Returns:
        (cob_date, factor_id, exposure, fret, pnl) — one row per date per
        factor the portfolio has exposure to; ``pnl`` in $mm.

    Raises:
        ValueError: ``estimates=True`` against a store with no estimate
            stream.
    """
    core = _core(model)
    end = end or start
    pub = T0_ESTIMATE if estimates else OFFICIAL

    rets = core.factor_returns(start, end, pub_type=pub)
    scale = scale_to_canonical("return",
                               core.conventions["return_convention"])
    rets = (rets.select(COB_DATE, FACTOR_ID,
                        (pl.col(VALUE) * scale).alias("fret")))

    history = core.loading_history(start, end, assets=portfolio.assets)
    holdings = portfolio.holdings.rename({VALUE: _POS})
    exp = (history.join(holdings, on=ASSET_ID)
           .group_by(COB_DATE, FACTOR_ID)
           .agg((pl.col(VALUE) * pl.col(_POS)).sum().alias("exposure")))

    out = (exp.join(rets, on=[COB_DATE, FACTOR_ID])
           .with_columns((pl.col("exposure") * pl.col("fret")).alias("pnl")))
    return _ordered(out.sort(COB_DATE), core.factors).sort(COB_DATE,
                                                           maintain_order=True)


def estimate_factor_returns(model, as_of: date) -> pl.DataFrame:
    """T0 estimation — the top of the pipeline: take the factor-mimicking
    portfolios and calculate the return on them.

    estimate_f = Σ_a w_{f,a} · r_a — FMP weights × same-day asset returns,
    in canonical daily decimal. This is what an ingest job would write as
    the T0_ESTIMATE stream; computing it here lets a check assert parity
    with the stored stream. Only factors with FMPs are estimable.

    Args:
        model: Core ``Model`` or ``ModelFacade``.
        as_of: The COB date to estimate, ``datetime.date``.

    Returns:
        (factor_id, estimate) in factor_seq order, canonical daily decimal.
    """
    core = _core(model)
    weights = core.fmp_weights(as_of)
    scale = scale_to_canonical("return",
                               core.conventions["return_convention"])
    arets = (core.asset_returns(as_of, as_of)
             .select(ASSET_ID, (pl.col(VALUE) * scale).alias("aret")))
    out = (weights.join(arets, on=ASSET_ID)
           .group_by(FACTOR_ID)
           .agg((pl.col(WEIGHT) * pl.col("aret")).sum().alias("estimate")))
    return _ordered(out, core.factors)


def exposure_change(model, portfolio: Portfolio, *,
                    start: date, end: date,
                    factors: list[str] | None = None,
                    by_asset: bool = False) -> pl.DataFrame:
    """Explain a change in portfolio exposures between two dates.

    The drill-down question — "this exposure moved, where is it coming
    from?" — answered as data: per factor by default, and per (factor,
    asset) with ``by_asset=True``, so the asset driving a factor move is
    one filter away. Holdings are held constant (this scaffold), so the
    whole change is loading drift; with dated holdings the same join
    decomposes into loading vs position terms.

    Args:
        model: Core ``Model`` or ``ModelFacade``.
        portfolio: Scope; holdings in $mm.
        start: Baseline COB date, ``datetime.date``.
        end: Comparison COB date, ``datetime.date``.
        factors: Subset of factor ids; None = all.
        by_asset: False = one row per factor; True = one row per
            (factor, asset) — the attribution of each factor's move.

    Returns:
        (factor_id[, asset_id], exposure_start, exposure_end, change) in
        factor_seq order; $mm per unit loading. Rows present on either
        date appear (absent side filled 0.0 — a position entering or
        leaving a factor is itself a change worth seeing).
    """
    core = _core(model)
    holdings = portfolio.holdings.rename({VALUE: _POS})

    def _contrib(d: date) -> pl.DataFrame:
        return (core.factor_loadings(d, assets=portfolio.assets,
                                     factors=factors)
                .join(holdings, on=ASSET_ID)
                .with_columns((pl.col(VALUE) * pl.col(_POS))
                              .alias("exposure_start"))
                .select(ASSET_ID, FACTOR_ID, "exposure_start"))

    merged = (_contrib(start)
              .join(_contrib(end).rename({"exposure_start": "exposure_end"}),
                    on=[ASSET_ID, FACTOR_ID], how="full", coalesce=True)
              # fill only the exposure columns: a frame-level fill_null(0.0)
              # would upcast the integer asset_id
              .with_columns(pl.col("exposure_start", "exposure_end")
                            .fill_null(0.0))
              .with_columns((pl.col("exposure_end")
                             - pl.col("exposure_start")).alias("change")))
    if by_asset:
        return _ordered(merged, core.factors).sort(
            FACTOR_ID, ASSET_ID, maintain_order=True)
    per_factor = merged.group_by(FACTOR_ID).agg(
        pl.col("exposure_start").sum(),
        pl.col("exposure_end").sum(),
        pl.col("change").sum())
    return _ordered(per_factor, core.factors)
