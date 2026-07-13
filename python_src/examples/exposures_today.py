"""Q: What are my factor exposures right now?

A book arrives keyed by vendor security ids (as positions usually do).
Resolve them through asset_xref, build the canonical Portfolio, and get
value-weighted exposures — largest first.

    python examples/exposures_today.py [--root DIR] [--model ID]
"""

from _bootstrap import setup

import polars as pl

from analytics import Portfolio, exposures
from modelfacade import ModelFacade


def main() -> None:
    root, model_id = setup(__doc__)
    fac = ModelFacade.load(model_id, root)

    # positions as they arrive: vendor ids -> $mm market values
    positions = {"AX0000001": 10.0, "AX0000002": 20.0, "AX0000003": -5.0}

    # resolve vendor ids to internal asset_ids via the xref dimension
    xref = fac.core.store.dim("asset_xref")
    id_map = dict(xref.filter(
        pl.col("vendor_asset_id").is_in(list(positions))
    ).select("vendor_asset_id", "asset_id").iter_rows())
    holdings = {id_map[v]: mv for v, mv in positions.items()}

    book = Portfolio.from_holdings("book", fac.core.dates()[1], holdings)
    print(book)

    exp = exposures(fac, book)
    print(exp.sort(pl.col("exposure").abs(), descending=True))


if __name__ == "__main__":
    main()
