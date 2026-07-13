"""Guided tour of the conventions & modelfacade packages.

Runs against the persistent demo micro store (built on first use under the
repo's data/demo/ — no real data needed), or against a real v2 store with
--root. Each step prints what it is doing and shows the result, so the
output reads as a tutorial. Step 7's cross-session part consumes the working
set persisted by the warming job — run  python warm_cache.py --demo  first
to see the full producer/consumer pipeline.

    python usage_example.py                  # demo micro store, from python_src/
    python usage_example.py --root DIR       # a real v2 store (local or s3://)
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import polars as pl


def section(n: int, title: str) -> None:
    print(f"\n{'=' * 72}\n{n}. {title}\n{'=' * 72}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", help="real v2 store root (default: micro store)")
    ap.add_argument("--model", default="AX_WW4_MH", help="model id with --root")
    args = ap.parse_args()

    # ------------------------------------------------------------------ 1
    section(1, "conventions: constants, adapter toolkit, units")

    # Canonical column names are shared string constants — code references
    # the constant, never re-types the literal.
    from conventions import ASSET_ID, COB_DATE, VALUE
    print(f"constants: COB_DATE={COB_DATE!r} ASSET_ID={ASSET_ID!r} VALUE={VALUE!r}")

    # The adapter toolkit sanitizes legacy naming at the boundary.
    from conventions import rename_snake, snake_case
    for messy in ("CobDate", "COB-DATE", "factor Group", "ASOFDATE"):
        print(f"snake_case({messy!r:15}) -> {snake_case(messy)!r}")

    legacy = pl.DataFrame({"AssetId": [1, 2], "FACTORGROUP": ["A", "B"]})
    print("rename_snake(legacy).columns ->", rename_snake(legacy).columns)

    # Unit conversions are executable multipliers into canonical units
    # (returns daily decimal, vol annualized decimal, cov annualized dec^2).
    from conventions import scale_to_canonical
    print("scale_to_canonical('specific_risk', 'ann_vol_pct') ->",
          scale_to_canonical("specific_risk", "ann_vol_pct"))
    print("scale_to_canonical('covariance',    'daily_var')   ->",
          scale_to_canonical("covariance", "daily_var"))

    # Identifier schemes are a closed enum matching asset_xref.vendor.
    from conventions import SecurityIDType, sec_id_col
    print("SecurityIDType.AXIOMA ->", SecurityIDType.AXIOMA,
          "| sec_id_col(BARRA) ->", sec_id_col(SecurityIDType.BARRA))

    # ------------------------------------------------------- store selection
    if args.root:
        root, model_id = args.root, args.model
    else:
        # No real store: use the persistent demo micro store (built on first
        # use) — the same one warm_cache.py --demo warms, so its persisted
        # working set is consumable in step 7.
        from modelfacade.selftest import MID, ensure_micro_store
        root, model_id = str(ensure_micro_store()), MID

    # ------------------------------------------------------------------ 2
    section(2, "discoverability: list_models() and describe()")

    from modelfacade import ModelFacade, list_models
    print(list_models(root))

    fac = ModelFacade.load(model_id, root)   # one line to data access
    for k, v in fac.describe().items():
        print(f"  {k}: {v}")

    # ------------------------------------------------------------------ 3
    section(3, "the strict core: always right or fails fast")

    core = fac.core                          # unwrap the strict layer
    latest = core.dates()[1]                 # datetime.date, as core demands

    long = core.factor_loadings(latest)      # long form, sparse, raw units
    print(f"core.factor_loadings({latest}) -> {long.height} sparse rows")
    print(long.head(3))

    try:
        core.factor_loadings(str(latest))    # a string date: core refuses
    except TypeError as e:
        print(f"core.factor_loadings('{latest}') -> TypeError: {e}")

    # ------------------------------------------------------------------ 4
    section(4, "the lenient facade: string dates, 'latest', wide one-liners")

    wide = fac.get_factor_loadings("latest")             # wide: 1 col/factor
    print("get_factor_loadings('latest') ->", wide.shape, "columns:",
          wide.columns[:6], "...")
    print(wide.head(3))

    same = fac.get_factor_loadings(str(latest))          # ISO string works
    print(f"get_factor_loadings('{latest}') equals date form:",
          same.equals(fac.get_factor_loadings(latest)))

    # ------------------------------------------------------------------ 5
    section(5, "vendor security ids resolve via asset_xref")

    picked = fac.get_factor_loadings(
        "latest", assets=["AX0000003"], sec_id_type=SecurityIDType.AXIOMA)
    print("assets=['AX0000003'] (AXIOMA) -> asset_id",
          picked[ASSET_ID].to_list())
    auto = fac.get_factor_loadings("latest", assets=["B0000004"])
    print("assets=['B0000004'] (scheme auto-detected) -> asset_id",
          auto[ASSET_ID].to_list())

    # ------------------------------------------------------------------ 6
    section(6, "canonical units out of the facade; raw out of the core")

    raw = core.specific_risk(latest, assets=[1])
    can = fac.get_specific_risk("latest", assets=[1])
    conv = core.conventions["specific_risk_convention"]
    print(f"core  (raw, {conv}): {raw[VALUE][0]}")
    print(f"facade (canonical annualized decimal): {can[VALUE][0]}")

    rets = fac.get_factor_returns()          # latest date, daily decimal
    print("get_factor_returns() head:")
    print(rets.head(3))

    # ------------------------------------------------------------------ 7
    section(7, "the pre-warm cache: load the working set once")

    session = ModelFacade.load(model_id, root)   # a fresh user session
    positions = [1, 2, 3]                        # 'the assets you hold'
    stats = session.warm(positions)              # YTD loadings+srisk+returns
    print("after warm(positions):", stats)

    session.get_factor_loadings("latest", assets=[1, 2])   # served from cache
    session.get_specific_risk("latest", assets=[3])        # served from cache
    print("after two covered requests:", session.cache.stats)

    # Working sets persist to disk (parquet + manifest), keyed by
    # (as-of COB date, model_id): <base>/usercache/<as_of>/<model_id>/.
    # The scheduled job (warm_cache.py) is the producer; this session is
    # the consumer — it starts hot from whatever the job persisted.
    later = ModelFacade.load(model_id, root)   # 'a later session'
    try:
        later.load_cache()                     # newest persisted set
        later.get_factor_loadings("latest", assets=[1, 2])
        print("fresh session after load_cache():", later.cache.stats)
    except FileNotFoundError:
        print("no persisted working set found for", model_id,
              "— run `python warm_cache.py --demo` first to see this step hit")

    # ------------------------------------------------------------------ 8
    section(8, "output='pandas' — conversion once, at the return boundary")

    try:
        pdfac = ModelFacade.load(model_id, root, output="pandas")
        frame = pdfac.get_factor_loadings("latest")
        print("get_factor_loadings('latest') returns:", type(frame))
        print(frame.head(3))
    except ModuleNotFoundError as e:
        print(f"(pandas not installed: {e})")

    # ------------------------------------------------------------------ 9
    section(9, "the layers convert both ways")

    rewrapped = ModelFacade(fac.core)        # wrap a core Model
    print("ModelFacade(fac.core).model_id ->", rewrapped.model_id)
    print("fac.core is the strict layer   ->", type(fac.core).__name__)

    print("\ndone.")


if __name__ == "__main__":
    main()
