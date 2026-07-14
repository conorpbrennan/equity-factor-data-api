"""Analytics runner: compute analytics at scale, persist results.

The evening counterpart to warm_cache.py: for every portfolio in the book
file, compute the standard analytics (exposures, PnL decomposition — flash
stream if requested and available — and the day-over-day exposure change)
and persist each result as parquet plus a manifest, keyed like the user
cache: <base>/analytics_results/<as_of>/<model_id>/<portfolio>/. Downstream
reports read persisted frames; they never recompute.

    python run_analytics.py --demo                      # micro store, try now
    python run_analytics.py --root DIR --model AX_WW4_MH \
                            --portfolios books.json     # real store

books.json maps portfolio name -> {asset_id: market value $mm}:

    { "pm_alpha": {"101": 12.5, "102": -4.0},
      "pm_beta":  {"205": 30.0} }

One portfolio failing does not stop the others; exit code is nonzero if any
failed. Base dir: $ANALYTICS_RESULTS_DIR or the system temp dir. As a cron
job it pairs with warm_cache.py in the same crontab.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import polars as pl

from analytics import (Portfolio, exposure_change, exposures,
                       pnl_decomposition)
from modelfacade import ModelFacade


def results_base() -> Path:
    base = os.environ.get("ANALYTICS_RESULTS_DIR", tempfile.gettempdir())
    return Path(base).expanduser() / "analytics_results"


def run_one(fac: ModelFacade, name: str, holdings: dict,
            flash: bool) -> bool:
    """Compute and persist the standard analytics for one portfolio.
    Never raises (cron-safe); returns success."""
    try:
        as_of = fac.core.dates()[1]
        book = Portfolio.from_holdings(
            name, as_of, {int(a): float(v) for a, v in holdings.items()})

        results = {"exposures": exposures(fac, book),
                   "pnl": pnl_decomposition(fac, book, start=as_of,
                                            estimates=flash)}
        # day-over-day change needs a previous COB; find it in the store
        prior = (fac.core.factor_returns(as_of - timedelta(days=7), as_of)
                 .filter(pl.col("cob_date") < as_of)["cob_date"])
        if len(prior):
            results["exposure_change"] = exposure_change(
                fac, book, start=prior.max(), end=as_of, by_asset=True)

        out = results_base() / as_of.isoformat() / fac.model_id / name
        out.mkdir(parents=True, exist_ok=True)
        for analytic, frame in results.items():
            frame.write_parquet(out / f"{analytic}.parquet")
        (out / "manifest.json").write_text(json.dumps({
            "portfolio": name, "model_id": fac.model_id,
            "as_of": as_of.isoformat(), "pnl_stream":
                "T0_ESTIMATE" if flash else "OFFICIAL",
            "analytics": {k: len(v) for k, v in results.items()},
        }, indent=2))
        rows = ", ".join(f"{k}={len(v)}" for k, v in results.items())
        print(f"OK    {name}: {rows}\n      -> {out}")
        return True
    except Exception as e:
        print(f"FAIL  {name}: {type(e).__name__}: {e}", file=sys.stderr)
        return False


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Compute and persist per-portfolio analytics.")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--aws", action="store_true",
                     help="the project S3 store (needs AWS_FACTOR_READER_* keys in env)")
    grp.add_argument("--root",
                     help="store root (default: $FACTOR_STORE_ROOT)")
    ap.add_argument("--model", default="AX_WW4_MH")
    ap.add_argument("--portfolios", help="JSON: name -> {asset_id: $mm}")
    ap.add_argument("--flash", action="store_true",
                    help="PnL on the T0 estimate stream instead of official")
    ap.add_argument("--demo", action="store_true",
                    help="run against the persistent demo micro store")
    args = ap.parse_args()
    if args.aws:
        from modelfacade.store import AWS_ROOT
        args.root = AWS_ROOT

    if args.demo:
        from modelfacade.selftest import MID, ensure_micro_store
        root, model_id = str(ensure_micro_store()), MID
        books = {"pm_alpha": {"1": 10.0, "2": 20.0},
                 "pm_beta": {"2": -5.0, "4": 8.0, "6": 3.0}}
        print(f"demo: micro store at {root}\n")
    else:
        if not args.portfolios:
            ap.error("--portfolios is required")
        root, model_id = args.root, args.model
        books = json.loads(Path(args.portfolios).expanduser().read_text())

    fac = ModelFacade.load(model_id, root)
    fac.warm([int(a) for h in books.values() for a in h])   # one warm, N books
    results = [run_one(fac, name, holdings, args.flash)
               for name, holdings in books.items()]
    print(f"\n{sum(results)}/{len(results)} portfolios processed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
