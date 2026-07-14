"""Q: What am I actually betting on, relative to my benchmark?

Portfolio arithmetic: positions minus benchmark is the active book, and the
active book goes through exactly the same analytics as its parents. Common
factor bets shared with the benchmark net toward zero; what remains is the
deliberate tilt.

    python examples/active_vs_benchmark.py [--aws | --root DIR] [--model ID]
"""

from _bootstrap import setup

import polars as pl

from analytics import Portfolio, exposures
from modelfacade import ModelFacade


def main() -> None:
    root, model_id = setup(__doc__)
    fac = ModelFacade.load(model_id, root)
    as_of = fac.core.dates()[1]

    book = Portfolio.from_holdings("book", as_of, {1: 10.0, 2: 20.0})
    bench = Portfolio.from_holdings("bench", as_of, {1: 15.0, 2: 15.0})
    active = book - bench
    print(book, bench, active, sep="\n")

    # same function, three portfolios — the analytics don't care which
    side_by_side = (
        exposures(fac, book).rename({"exposure": "book"})
        .join(exposures(fac, bench).rename({"exposure": "bench"}),
              on="factor_id", how="full", coalesce=True)
        .join(exposures(fac, active).rename({"exposure": "active"}),
              on="factor_id", how="full", coalesce=True)
        .fill_null(0.0))
    print(side_by_side)
    print("note: the shared market bet nets to ~0 in the active column —",
          "what's left is the deliberate tilt")


if __name__ == "__main__":
    main()
