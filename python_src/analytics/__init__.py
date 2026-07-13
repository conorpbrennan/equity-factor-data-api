"""Canonical portfolio + stateless analytics (roadmap project 3).

One Portfolio class for every portfolio-shaped thing — booked positions, a
benchmark, a hypothetical basket — with arithmetic to compose them
(positions - benchmark = the active portfolio). Analytics are stateless
functions (model, portfolio, ...) -> DataFrame: the model supplies data, the
portfolio supplies scope, the function computes; nothing hides state.

The two fundamentals every downstream report needs in some flavour:

    exposures(model, portfolio)                 # value-weighted loadings
    pnl_decomposition(model, portfolio, ...)    # per-factor PnL contributions
    pnl_decomposition(..., estimates=True)      # same number at T0: flash PnL
    exposure_change(model, portfolio, ...)      # "where is this move coming
                                                #  from?" — per factor, and
                                                #  per asset with by_asset=True

Functions accept the strict core Model or a ModelFacade (unwrapped via
.core). Units follow conventions: money in millions USD, returns daily
decimal — so exposures are $mm per unit loading and PnL is $mm.
"""

from .portfolio import Portfolio
from .functions import (estimate_factor_returns, exposure_change, exposures,
                        pnl_decomposition)

__all__ = ["Portfolio", "estimate_factor_returns", "exposure_change",
           "exposures", "pnl_decomposition"]
