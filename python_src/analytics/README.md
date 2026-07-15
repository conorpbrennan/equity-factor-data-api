# analytics — canonical portfolio + stateless functions

## Why this project

Analytics logic today is split across Python scripts and stored SQL
procedures, and there is no canonical representation of a portfolio — every
system passes around dataframes with its own folklore about column names and
conventions. The fix has two parts: one Portfolio class for every
portfolio-shaped thing, and analytics as stateless functions over it, so the
same code path serves booked positions, benchmarks, hypotheticals, and any
arithmetic combination of them.

## Proposed change

`Portfolio` (portfolio.py) — one immutable class:

- holdings as internal asset ids + market values in $mm (conventions units);
  weights derived on demand, never stored
- built from a dict or any frame-like; duplicates summed, zeros dropped
- arithmetic aligned on asset_id: `positions - benchmark` is the active
  portfolio, `2 * book` scales, different as-of dates refuse to combine
- strict about dates like the core layer (`datetime.date` only)

`RiskProfile` (riskprofile.py) — the PAS requirement (2026-07-15): an
arbitrary combination of factor exposures and specific-risk positions, not
representable as securities, analyzable like a portfolio:

- built from explicit exposures (`from_exposures`) or materialized from a
  portfolio's canonical first analytic step (`from_portfolio(model, port)`)
- accepted by `exposures()` (validated pass-through) and
  `pnl_decomposition()` (exposures fixed across the window — that fixity is
  what a profile means); `exposure_change` is Portfolio-only
- same arithmetic as Portfolio, aligned on factor_id / asset_id

Stateless functions (functions.py) — `(model, portfolio, ...) -> DataFrame`,
where scope is a `Portfolio` or a `RiskProfile`:

- `exposures(model, portfolio)` — value-weighted loadings,
  exposure_f = Σ value_i · loading_{i,f}, in $mm per unit loading
- `pnl_decomposition(model, portfolio, start=, end=)` — per-factor PnL
  contributions, exposures recomputed from each date's loadings, returns
  canonicalized, PnL in $mm
- `pnl_decomposition(..., estimates=True)` — the same decomposition on the
  T0 estimate stream: **the flash PnL**, available the evening it describes,
  one keyword apart from the official number
- `exposure_change(model, portfolio, start=, end=, by_asset=)` — "this
  moved, where is it coming from?": per-factor changes, attributed per
  asset on demand
- `volatility(model, portfolio)` — annualized dollar vol at the scope's
  as-of date, decomposed: vol² = x'Σx + Σ (value·srisk)², returned as
  (component, variance, vol) rows for factor / specific / total. Works on
  a Portfolio or a RiskProfile — this is the analytic the profile's
  specific leg exists for
- `estimate_factor_returns(model, as_of)` — the top of the T0 pipeline:
  FMP weights × same-day asset returns; the selftest asserts parity with
  the stored estimate stream

The runner (`python_src/run_analytics.py`): book file in (portfolio →
holdings), per-portfolio analytics out as parquet + manifest under
`analytics_results/<as_of>/<model>/<portfolio>/` — official or `--flash`
stream — so downstream reports read persisted frames rather than
recomputing. Cron-safe; `--demo` runs it against the micro store.

Both accept the strict core `Model` or a `ModelFacade` (unwrapped via
`.core`) and convert units themselves through the conventions library, so
they compose with either layer without double conversion.

## What the scaffold shows

- `python -m analytics selftest` — 7 exact-number checks against the
  deterministic micro store (construction, arithmetic, exposure cells, both
  PnL streams, statelessness, change attribution, estimation parity).
- The two "certain building blocks" — exposures and PnL decomposition —
  expressed in the target pattern, ready to be measured against real
  stored-procedure outputs when access exists.

## Open questions

- **Holdings drift.** The decomposition holds values constant over the
  window (buy-and-hold, no trades). Real attribution needs daily holdings —
  does the Portfolio grow a time dimension, or do callers pass one Portfolio
  per date?
- **Residual PnL.** Factor contributions only so far — but asset returns now
  exist in the store (added for T0 estimation), so specific (residual) PnL
  is the natural next analytic: asset PnL minus the factor contributions.
- **Wraps or replaces?** The existing core library has a portfolio class
  with some contested design decisions — whether this canonical class wraps
  it or replaces it is a live question.
- **Ownership split.** Core, tested analytics vs the risk team's ad-hoc
  sandbox — same pattern, different deployment ceremony; where exactly is
  the line?
- **Config object.** The target signature is (model, portfolio, config);
  this scaffold uses explicit kwargs until real config shapes emerge.
