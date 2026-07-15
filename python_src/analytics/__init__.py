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
    volatility(model, portfolio)                # x'Σx + specific leg, $mm
                                                #  annualized, decomposed

Scope can also be a RiskProfile (riskprofile.py) — an arbitrary combination
of factor exposures and specific-risk positions, not representable as
securities (a PAS requirement): exposures() passes its exposures through
validated, pnl_decomposition() prices them against the return streams, and
RiskProfile.from_portfolio(model, port) materializes the canonical first
step of every analytic. exposure_change is Portfolio-only — profile
exposures are fixed by definition.

Functions accept the strict core Model or a ModelFacade (unwrapped via
.core). Units follow conventions: money in millions USD, returns daily
decimal — so exposures are $mm per unit loading and PnL is $mm.
"""

from .portfolio import Portfolio
from .riskprofile import RiskProfile
from .functions import (estimate_factor_returns, exposure_change, exposures,
                        pnl_decomposition, volatility)

__all__ = ["Portfolio", "RiskProfile", "estimate_factor_returns",
           "exposure_change", "exposures", "pnl_decomposition",
           "volatility"]
