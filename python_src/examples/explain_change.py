"""Q: My exposure moved — which factor, and which asset drove it?

The drill-down: factor-level changes first, then attribute the biggest
mover to the assets behind it, and say the answer in a sentence. This is
the "drilling into risk changes for a day" exercise, as one function call.

    python examples/explain_change.py [--root DIR] [--model ID]
"""

from _bootstrap import setup

import polars as pl

from analytics import Portfolio, exposure_change
from modelfacade import ModelFacade


def main() -> None:
    root, model_id = setup(__doc__)
    fac = ModelFacade.load(model_id, root)
    start, end = fac.core.dates()             # full stored range

    book = Portfolio.from_holdings("book", end, {1: 10.0, 2: 20.0})

    # 1. which factors moved?
    chg = exposure_change(fac, book, start=start, end=end)
    print(f"exposure changes, {start} -> {end} ($mm per unit loading):")
    print(chg.sort(pl.col("change").abs(), descending=True))

    # 2. attribute the biggest mover to assets
    top = chg.sort(pl.col("change").abs(), descending=True)["factor_id"][0]
    by = (exposure_change(fac, book, start=start, end=end, by_asset=True)
          .filter(pl.col("factor_id") == top)
          .sort(pl.col("change").abs(), descending=True))
    print(f"\nwho drove {top}?")
    print(by)

    # 3. the answer, in words
    move = chg.filter(pl.col("factor_id") == top)["change"][0]
    driver = by.row(0, named=True)
    print(f"\n=> {top} moved {move:+.3f}mm; largest contributor: asset "
          f"{driver['asset_id']} ({driver['change']:+.3f}mm of it)")


if __name__ == "__main__":
    main()
