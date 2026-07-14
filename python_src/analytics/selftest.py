"""Analytics selftest against the modelfacade micro store.

Reuses the fabricated two-model store (deterministic loading_value, official
0.5 / estimate 0.6 daily_pct returns), so every check asserts exact numbers:
portfolio arithmetic, value-weighted exposures cell by cell, and PnL
decomposition on both publication streams. One PASS/FAIL line per contract.

    python -m analytics selftest
"""

from __future__ import annotations

import tempfile
import traceback
from datetime import date
from pathlib import Path

import polars as pl

from modelfacade import Model, ModelFacade, Store
from modelfacade.selftest import DATES, MID, build_micro_store, loading_value

from .functions import (estimate_factor_returns, exposure_change, exposures,
                        pnl_decomposition)
from .portfolio import Portfolio

# The test book: long 10mm of asset 1, 20mm of asset 2, at the last COB.
PORT = {1: 10.0, 2: 20.0}
BENCH = {1: 15.0, 2: 15.0}


def _port(name="book", holdings=PORT) -> Portfolio:
    return Portfolio.from_holdings(name, DATES[-1], holdings)


def check_portfolio_construction(_root):
    """One canonical class for any portfolio-shaped thing: dict or frame in,
    duplicates summed, zero holdings dropped, values $mm; weights derived
    from gross on demand; non-date as_of rejected like the core layer."""
    p = Portfolio.from_holdings("book", DATES[-1],
                                {1: 6.0, 2: 20.0, 3: 0.0})
    q = Portfolio.from_holdings("dup", DATES[-1],
                                pl.DataFrame({"asset_id": [1, 1, 2],
                                              "value": [3.0, 3.0, 20.0]}))
    assert p.holdings.equals(q.holdings)              # dup summed, zero gone
    assert p.assets == [1, 2] and p.net == 26.0 and p.gross == 26.0
    w = q.weights()
    assert abs(w.filter(pl.col("asset_id") == 2)["weight"][0]
               - 20.0 / 26.0) < 1e-12
    try:
        Portfolio.from_holdings("bad", "2025-01-15", PORT)
        raise AssertionError("string as_of accepted")
    except TypeError:
        pass


def check_portfolio_arithmetic(_root):
    """Arithmetic composes portfolios aligned on asset_id: positions minus
    benchmark is the active book (disjoint assets fill at zero), scalar
    multiply scales values, and combining different as-of dates refuses —
    alignment across dates must be an explicit decision."""
    active = _port() - _port("bench", BENCH)
    got = dict(zip(active.holdings["asset_id"], active.holdings["value"]))
    assert got == {1: -5.0, 2: 5.0} and active.net == 0.0
    disjoint = _port() + _port("other", {5: 7.0})
    assert disjoint.assets == [1, 2, 5]               # outer alignment
    assert (2 * _port()).net == 60.0
    assert (-_port()).net == -30.0
    try:
        _port() - Portfolio.from_holdings("stale", DATES[0], BENCH)
        raise AssertionError("as_of mismatch accepted")
    except ValueError:
        pass


def check_exposures(root):
    """exposure_f = Σ value_i · loading_{i,f}, straight from the sparse
    store rows: one-hots give the position value itself (asset 1 is IND01,
    asset 2 IND02), styles give the deterministic generator numbers, and
    the market factor sums the book. Core and facade give the same frame;
    rows come back in factor_seq order."""
    core = Model(Store.open(str(root)), MID)
    exp = exposures(core, _port())
    got = dict(zip(exp["factor_id"], exp["exposure"]))
    day = len(DATES) - 1
    assert abs(got["MARKET_SENSITIVITY"]
               - (10 * loading_value(1, 0, day)
                  + 20 * loading_value(2, 0, day))) < 1e-9
    assert abs(got["MT_MOMENTUM"] - 5.57) < 1e-9
    assert got["IND01"] == 10.0 and got["IND02"] == 20.0
    assert got["MARKET"] == 30.0
    assert exp["factor_id"].to_list() == list(got)     # factor_seq order
    via_facade = exposures(ModelFacade.load(MID, str(root)), _port())
    assert via_facade.equals(exp)                      # .core unwrap
    subset = exposures(core, _port(), factors=["MARKET"])
    assert subset.height == 1 and subset["exposure"][0] == 30.0


def check_pnl_official(root):
    """contribution_{f,d} = exposure_{f,d} · return_{f,d}: exposures are
    recomputed from each date's loadings (they drift with the generator's
    AR path), official returns are 0.5 daily_pct = 0.005 canonical, and
    PnL comes out in $mm — one row per date per factor with exposure."""
    core = Model(Store.open(str(root)), MID)
    pnl = pnl_decomposition(core, _port(), start=DATES[0], end=DATES[-1])
    assert pnl.height == len(DATES) * 6                # 6 loaded factors
    day0 = pnl.filter((pl.col("cob_date") == DATES[0])
                      & (pl.col("factor_id") == "MT_MOMENTUM"))
    exp0 = 10 * loading_value(1, 1, 0) + 20 * loading_value(2, 1, 0)
    assert abs(day0["exposure"][0] - exp0) < 1e-9      # 5.3 on day 0
    assert abs(day0["pnl"][0] - exp0 * 0.005) < 1e-12
    market = pnl.filter(pl.col("factor_id") == "MARKET")
    assert abs(market["pnl"].sum() - 30.0 * 0.005 * len(DATES)) < 1e-9


def check_pnl_flash(root):
    """The flash-PnL story: the same decomposition with estimates=True runs
    on the T0 estimate stream (0.6 daily_pct = 0.006) — available the
    evening it describes, one keyword apart from the official number. The
    functions are stateless: the portfolio is untouched by either call."""
    core = Model(Store.open(str(root)), MID)
    book = _port()
    before = book.holdings.clone()
    official = pnl_decomposition(core, book, start=DATES[-1])
    flash = pnl_decomposition(core, book, start=DATES[-1], estimates=True)
    o = official.filter(pl.col("factor_id") == "MARKET")
    f = flash.filter(pl.col("factor_id") == "MARKET")
    assert abs(o["pnl"][0] - 30.0 * 0.005) < 1e-12     # official
    assert abs(f["pnl"][0] - 30.0 * 0.006) < 1e-12     # flash, same exposure
    assert f["exposure"][0] == o["exposure"][0]
    assert book.holdings.equals(before)                # stateless


def check_exposure_change(root):
    """The drill-down: exposure_change explains a move per factor, and
    by_asset=True attributes each factor's move to the assets driving it.
    In the micro store style loadings drift +0.001/day, so over 9 days the
    book's style exposures move by (10+20)·0.009 = 0.27, one-hots don't
    move, and the by-asset split is exactly 0.09 (asset 1) + 0.18
    (asset 2)."""
    core = Model(Store.open(str(root)), MID)
    chg = exposure_change(core, _port(), start=DATES[0], end=DATES[-1])
    got = dict(zip(chg["factor_id"], chg["change"]))
    assert abs(got["MT_MOMENTUM"] - 0.27) < 1e-9        # 30mm · 0.001 · 9d
    assert abs(got["MARKET_SENSITIVITY"] - 0.27) < 1e-9
    assert got["IND01"] == 0.0 and got["MARKET"] == 0.0  # one-hots stable
    by = exposure_change(core, _port(), start=DATES[0], end=DATES[-1],
                         by_asset=True)
    assert by["asset_id"].dtype == pl.Int32             # fill must not upcast
    mt = by.filter(pl.col("factor_id") == "MT_MOMENTUM")
    per_asset = dict(zip(mt["asset_id"], mt["change"]))
    assert abs(per_asset[1] - 0.09) < 1e-9              # 10mm · 0.009
    assert abs(per_asset[2] - 0.18) < 1e-9              # 20mm · 0.009
    assert abs(mt["change"].sum() - got["MT_MOMENTUM"]) < 1e-12   # adds up


def check_t0_estimation(root):
    """The top of the T0 pipeline: estimate_factor_returns computes FMP
    weights × same-day asset returns. The micro store's asset returns vary
    by asset (0.575..0.625 daily_pct) but their equal-weight FMP average is
    exactly 0.6 — so the computed estimates must equal the stored
    T0_ESTIMATE stream (0.006 canonical), factor for factor. Only factors
    with FMPs (the styles) are estimable."""
    core = Model(Store.open(str(root)), MID)
    arets = core.asset_returns(DATES[-1], DATES[-1])
    assert arets["value"].min() != arets["value"].max()   # genuinely varying
    est = estimate_factor_returns(core, DATES[-1])
    assert est["factor_id"].to_list() == ["MARKET_SENSITIVITY",
                                          "MT_MOMENTUM", "VALUE"]
    stored = (ModelFacade.load(MID, str(root), output="polars")
              .get_factor_returns(estimates=True,
                                  factors=est["factor_id"].to_list()))
    for f in est["factor_id"]:
        computed = est.filter(pl.col("factor_id") == f)["estimate"][0]
        placed = stored.filter(pl.col("factor_id") == f)["value"][0]
        assert abs(computed - placed) < 1e-12, (f, computed, placed)
        assert abs(computed - 0.006) < 1e-12


CHECKS = [
    ("portfolio: canonical construction, weights, strict as_of",
     check_portfolio_construction),
    ("portfolio: arithmetic — active book, alignment, date guard",
     check_portfolio_arithmetic),
    ("exposures: value-weighted loadings, exact cells, facade unwrap",
     check_exposures),
    ("pnl: per-factor decomposition, official stream, $mm",
     check_pnl_official),
    ("pnl: flash via estimates=True, stateless functions",
     check_pnl_flash),
    ("exposure_change: per-factor move, by-asset attribution",
     check_exposure_change),
    ("t0 estimation: FMP × asset returns == stored estimate stream",
     check_t0_estimation),
]


def main() -> int:
    failed = 0
    with tempfile.TemporaryDirectory(prefix="analytics_selftest_") as tmp:
        root = Path(tmp)
        build_micro_store(root)
        for name, fn in CHECKS:
            try:
                fn(root)
                print(f"PASS  {name}")
            except Exception:
                failed += 1
                print(f"FAIL  {name}")
                traceback.print_exc()
    print(f"\n{len(CHECKS) - failed}/{len(CHECKS)} checks passed")
    return 1 if failed else 0
