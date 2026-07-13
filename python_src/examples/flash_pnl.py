"""Q: What's my factor PnL tonight, before the official numbers land?

Official factor returns publish overnight; the T0 estimate stream exists
same-day. The same decomposition runs on either — one keyword apart — so the
flash number and tomorrow's official number come from one code path, not a
bolted-on side system.

    python examples/flash_pnl.py [--root DIR] [--model ID]
"""

from _bootstrap import setup

import polars as pl

from analytics import Portfolio, pnl_decomposition
from modelfacade import ModelFacade


def main() -> None:
    root, model_id = setup(__doc__)
    fac = ModelFacade.load(model_id, root)
    today = fac.core.dates()[1]

    book = Portfolio.from_holdings("book", today, {1: 10.0, 2: 20.0})

    flash = pnl_decomposition(fac, book, start=today, estimates=True)
    official = pnl_decomposition(fac, book, start=today)   # tomorrow's view

    both = (flash.select("factor_id", pl.col("pnl").alias("flash_pnl_mm"))
            .join(official.select("factor_id",
                                  pl.col("pnl").alias("official_pnl_mm")),
                  on="factor_id"))
    print(f"factor PnL for {today} ($mm):")
    print(both)
    print(f"totals: flash {both['flash_pnl_mm'].sum():.4f}mm, "
          f"official {both['official_pnl_mm'].sum():.4f}mm")


if __name__ == "__main__":
    main()
