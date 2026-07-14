---
name: factor-data
description: Load factor-model data (loadings, factor returns incl. T0 estimates, covariance, specific risk) and run portfolio analytics (exposures, PnL decomposition / flash PnL, exposure-change drill-down) using this repo's modelfacade and analytics packages. Use when asked for factor exposures, portfolio risk data, PnL attribution, flash PnL, "what models are available", or to explain why an exposure changed.
---

# Factor data & portfolio analytics

Answer factor-model data questions by writing and running short Python
snippets against the `modelfacade` and `analytics` packages. Prefer running
code and showing the resulting frame over describing what code would do.

## Setup (every snippet)

Run from `python_src/` with the repo venv:

```bash
cd python_src && ../.venv/bin/python - <<'EOF'
# snippet here
EOF
```

Store root: `$FACTOR_STORE_ROOT`, or pass `root=` explicitly. If no real
store is mounted, use the demo micro store:

```python
from modelfacade.selftest import ensure_micro_store, MID
root, model_id = str(ensure_micro_store()), MID   # AX_TEST1_MH, 6 assets
```

**Start hot when a working set exists.** On an s3:// store, plain
`ModelFacade.load()` contacts the store (and launches the in-region query
box) just to read dimensions. Prefer the offline cold start — it touches
nothing until a request falls outside the saved coverage:

```python
try:
    fac = ModelFacade.from_cache(model_id, root)   # dims + facts from disk
except FileNotFoundError:
    fac = ModelFacade.load(model_id, root)         # no saved set — go live
```

`from_cache` freezes `'latest'` at the set's as-of date; fall back to
`load()` when the question needs fresher data than the saved set.

## Data access — one-liners

```python
from modelfacade import ModelFacade, list_models

list_models(root)                                  # every model + conventions
fac = ModelFacade.load(model_id, root)             # lenient user layer
fac.describe()                                     # styles, dates, raw units

fac.get_factor_loadings("latest")                  # wide, 1 col per factor
fac.get_factor_loadings("2025-01-06", assets=["AX0000003"])  # vendor ids ok
fac.get_specific_risk("latest")                    # canonical ann. decimal vol
fac.get_factor_returns("2025-01-02", "2025-01-15") # canonical daily decimal
fac.get_factor_returns(estimates=True)             # T0 estimate stream
fac.get_covariance("latest")                       # canonical ann. decimal²
```

Dates: `date`, `'YYYY-MM-DD'`, or `'latest'`. Facade output is canonical
units (returns daily decimal, vol annualized decimal, money $mm);
`fac.core` is the strict layer with raw vendor values. `output="pandas"`
on `load()` if a pandas frame is wanted.

## Portfolios & analytics

```python
from datetime import date
from analytics import Portfolio, exposures, pnl_decomposition, exposure_change

book  = Portfolio.from_holdings("book",  date(2025, 1, 15), {1: 10.0, 2: 20.0})
bench = Portfolio.from_holdings("bench", date(2025, 1, 15), {1: 15.0, 2: 15.0})
active = book - bench                     # the active portfolio

exposures(fac, active)                    # $mm per unit loading
pnl_decomposition(fac, book, start=date(2025, 1, 2), end=date(2025, 1, 15))
pnl_decomposition(fac, book, start=date(2025, 1, 15), estimates=True)  # flash

from analytics import estimate_factor_returns
estimate_factor_returns(fac, date(2025, 1, 15))   # T0 estimation itself:
                                                  # FMP weights × asset returns
```

For batch work ("run analytics for all my books and persist the results"),
use the runner: `python run_analytics.py --portfolios books.json`
(`--flash` for the estimate stream, `--demo` to try it).

Holdings are internal asset ids → $mm market values. To start from vendor
ids, resolve them first:
`ids = fac._resolve_assets(["AX0000001"], None)` or build the mapping from
`fac.core.store.dim("asset_xref")`.

## Recipe: "explain why this exposure changed"

The drill-down question. Factor level first, then attribute to assets:

```python
import polars as pl

chg = exposure_change(fac, book, start=date(2025, 1, 2), end=date(2025, 1, 15))
chg.sort("change", descending=True)       # which factors moved

by = exposure_change(fac, book, start=date(2025, 1, 2), end=date(2025, 1, 15),
                     by_asset=True)
by.filter(pl.col("factor_id") == "MT_MOMENTUM")   # which assets drove it
```

Answer in words after showing the frame: name the factor(s) with the
largest |change| and the asset contributions behind each.

## Recipe: "flash PnL for today"

```python
flash = pnl_decomposition(fac, book, start=<latest cob date>, estimates=True)
```

Official vs flash differ only by `estimates=` — same exposures, different
return stream (`type = OFFICIAL | T0_ESTIMATE`). If the store predates the
type column, `estimates=True` raises — report that the store carries no
estimate stream; do not substitute official numbers.

## Worked examples

`python_src/examples/` holds one runnable script per question —
exposures_today, active_vs_benchmark, flash_pnl, explain_change,
morning_workflow. When a request matches one, adapt that script rather
than writing from scratch.

## Gotchas

- Run as a module or from `python_src/` — the packages are import-rooted there.
- `Portfolio` and the core layer take `datetime.date` only; the facade also
  takes strings. Never pass datetimes to `Portfolio.from_holdings`.
- PnL decomposition holds positions constant over the window (buy-and-hold).
- Cache: `fac.warm(assets)` then repeated covered queries are memory-served;
  `fac.load_cache()` adopts a set persisted by `python warm_cache.py`, and
  `ModelFacade.from_cache(model_id, root)` cold-starts from one with zero
  store contact (see Setup).
- Verify claims with `python -m modelfacade selftest` (12 checks) and
  `python -m analytics selftest` (6 checks).
