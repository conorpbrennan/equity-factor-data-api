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
import textwrap
from datetime import date
from pathlib import Path

import polars as pl

from modelfacade import inventory


def section(n: int, title: str, desc: str) -> None:
    """Header plus a wrapped explanation, so the output narrates itself."""
    print(f"\n{'=' * 72}\n{n}. {title}\n{'=' * 72}")
    print(textwrap.fill(" ".join(desc.split()), width=72), end="\n\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--aws", action="store_true",
                     help="the project S3 store (needs AWS_FACTOR_READER_* keys in env)")
    grp.add_argument("--root", help="real v2 store root (default: micro store)")
    ap.add_argument("--model", default=inventory.DEFAULT_MODEL,
                    help="model id with --root")
    args = ap.parse_args()
    if args.aws:
        from modelfacade.store import AWS_ROOT
        args.root = AWS_ROOT

    # ------------------------------------------------------------------ 1
    section(1, "conventions: constants, adapter toolkit, units", """
        A convention is only real if code can import it. Column names are
        shared string constants (reference the constant, never re-type the
        literal); legacy naming is sanitized at the boundary by the adapter
        toolkit; unit conventions are executable conversions — every one a
        multiplier into canonical units; identifier schemes are plain strings
        with shared constants for the usual ones
        so 'barra' vs 'Barra' vs 'BARRA_ID' cannot drift.""")

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

    # Identifier schemes are plain strings matching asset_xref.vendor —
    # constants for the usual ones, any string for an odd one-off.
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
    section(2, "discoverability: list_models() and describe()", """
        A new user should not need tribal knowledge to orient. list_models()
        reads the store's model_master — every model with its vendor, region,
        size, and raw unit conventions. describe() answers the first
        questions about one model: which factors are styles, what date range
        is loaded, what units the vendor publishes in, whether it is a
        custom variant of a base model.""")

    from modelfacade import ModelFacade, list_models
    print(list_models(root))

    fac = ModelFacade.load(model_id, root)   # one line to data access
    for k, v in fac.describe().items():
        print(f"  {k}: {v}")

    # ------------------------------------------------------------------ 3
    section(3, "the strict core: always right or fails fast", """
        Model is the layer core systems build on: datetime.date only (a COB
        has no time — even datetimes are rejected), internal integer asset
        ids only, factor ids validated against the master, values exactly as
        the vendor published them. It never coerces — a wrong input is a
        loud TypeError pointing you at the facade, not a silent guess.
        Loadings come back long-form and sparse: only the nonzero rows.""")

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
    section(4, "the lenient facade: string dates, 'latest', wide one-liners", """
        ModelFacade wraps a core Model and accepts what humans actually
        type: ISO strings, 'latest', datetimes. Output is wide by default —
        the sparse long rows pivot to one column per factor in factor_seq
        order, absent one-hots filled with 0.0 — which is the shape an
        analysis notebook wants. Same data as the core, one line instead of
        boilerplate.""")

    wide = fac.get_factor_loadings("latest")             # wide: 1 col/factor
    print("get_factor_loadings('latest') ->", wide.shape, "columns:",
          wide.columns[:6], "...")
    print(wide.head(3))

    same = fac.get_factor_loadings(str(latest))          # ISO string works
    print(f"get_factor_loadings('{latest}') equals date form:",
          same.equals(fac.get_factor_loadings(latest)))

    # ------------------------------------------------------------------ 5
    section(5, "vendor security ids resolve via asset_xref", """
        Positions rarely arrive keyed by internal ids. The facade accepts
        vendor identifiers and resolves them through the asset_xref mapping
        table — pin the scheme with sec_id_type=, or let it auto-detect when
        the ids are unambiguous. An unknown id raises immediately; it never
        silently drops out of the result.""")

    # demo ids looked up from assets actually covered at the latest date,
    # so the tour holds on any store, not just the micro fixture
    xref = fac.core.store.dim("asset_xref")
    a1, a2 = wide[ASSET_ID][0], wide[ASSET_ID][1]
    ax_id, b_id = [
        xref.filter((pl.col("vendor") == vendor)
                    & (pl.col(ASSET_ID) == a))["vendor_asset_id"][0]
        for vendor, a in (("AXIOMA", a1), ("BARRA", a2))]
    picked = fac.get_factor_loadings(
        "latest", assets=[ax_id], sec_id_type=SecurityIDType.AXIOMA)
    print(f"assets=[{ax_id!r}] (AXIOMA) -> asset_id",
          picked[ASSET_ID].to_list())
    auto = fac.get_factor_loadings("latest", assets=[b_id])
    print(f"assets=[{b_id!r}] (scheme auto-detected) -> asset_id",
          auto[ASSET_ID].to_list())

    # ------------------------------------------------------------------ 6
    section(6, "canonical units out of the facade; raw out of the core", """
        Each vendor publishes in its own units; this model stores specific
        risk as annualized percent. The core hands the raw number back
        untouched (with the convention exposed as metadata); the facade
        converts once at the return boundary using the conventions library,
        so every facade user sees the same canonical units — annualized
        decimal vol, daily decimal returns — whatever the vendor did.
        Factor returns also carry two publication streams — vendor official
        and same-day T0 estimate — as ordinary rows discriminated by the
        type column (orthogonal to version_id, which handles restatements):
        the toggle below is an equality filter on that column, never a
        join, and a store predating the column still serves official while
        refusing estimates loudly.""")

    raw = core.specific_risk(latest, assets=[1])
    can = fac.get_specific_risk("latest", assets=[1])
    conv = core.conventions["specific_risk_convention"]
    print(f"core  (raw, {conv}): {raw[VALUE][0]}")
    print(f"facade (canonical annualized decimal): {can[VALUE][0]}")

    rets = fac.get_factor_returns()          # latest date, daily decimal
    print("get_factor_returns() head — official stream (default):")
    print(rets.head(3))

    # Factor returns carry two publication streams — vendor official and
    # same-day T0 estimates — discriminated by the type column. The toggle
    # is an equality filter on that column, never a join.
    try:
        est = fac.get_factor_returns(estimates=True)
        print("get_factor_returns(estimates=True) — T0 estimate stream:")
        print(est.head(3))
    except ValueError as e:
        print(f"get_factor_returns(estimates=True) -> refused: {e}")

    # ------------------------------------------------------------------ 7
    section(7, "the pre-warm cache: load the working set once", """
        Not query-result caching: most questions hit a predictable working
        set — YTD data for the assets you hold, in the model you care about.
        warm() loads that set once; any request covered by it (subset dates,
        subset assets) is served from memory, anything outside falls through
        to the store and is still correct. Working sets persist to parquet
        keyed by (as-of date, model): the scheduled job (warm_cache.py) is
        the producer, and the fresh session below consumes what it saved.""")

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
    section(8, "output='pandas' — conversion once, at the return boundary", """
        The dataframe library the user layer speaks is a per-facade setting,
        not an architecture decision: internals stay polars/Arrow throughout
        (core, cache, store reads), and the conversion to pandas happens
        exactly once, as the frame is handed back. Requesting pandas without
        it installed fails with an install hint, not a stack trace.""")

    try:
        pdfac = ModelFacade.load(model_id, root, output="pandas")
        frame = pdfac.get_factor_loadings("latest")
        print("get_factor_loadings('latest') returns:", type(frame))
        print(frame.head(3))
    except ModuleNotFoundError as e:
        print(f"(pandas not installed: {e})")

    # ------------------------------------------------------------------ 9
    section(9, "the layers convert both ways", """
        The two layers are separate objects with an explicit bridge:
        facade.core unwraps the strict Model for handing into core
        computations; ModelFacade(model) wraps one back up for interactive
        use. Because the user cache lives only on the facade, its leniency
        can never leak into a core computation.""")

    rewrapped = ModelFacade(fac.core)        # wrap a core Model
    print("ModelFacade(fac.core).model_id ->", rewrapped.model_id)
    print("fac.core is the strict layer   ->", type(fac.core).__name__)

    # ------------------------------------------------------------------ 10
    section(10, "analytics: canonical Portfolio, exposures, flash PnL", """
        One Portfolio class for every portfolio-shaped thing — booked
        positions, a benchmark, a hypothetical — with arithmetic aligned
        on asset_id: positions minus benchmark is the active book, fed to
        exactly the same analytics. The analytics themselves are stateless
        functions (model, portfolio) -> DataFrame: exposures are
        value-weighted loadings in $mm, PnL decomposition multiplies each
        date's exposures by that date's factor returns — and with
        estimates=True the same decomposition runs on the T0 stream: the
        flash PnL, available the evening it describes.""")

    from analytics import Portfolio, exposures, pnl_decomposition

    book = Portfolio.from_holdings("book", latest, {1: 10.0, 2: 20.0})
    bench = Portfolio.from_holdings("bench", latest, {1: 15.0, 2: 15.0})
    active = book - bench                    # the active portfolio
    print(book, "\n", bench, "\n", active, sep="")

    print("\nexposures(model, active) — $mm per unit loading:")
    print(exposures(core, active))

    print("PnL decomposition, official vs flash (same day, one keyword):")
    official = pnl_decomposition(core, book, start=latest)
    try:
        flash = pnl_decomposition(core, book, start=latest, estimates=True)
        comparison = (official.select("factor_id", pl.col("pnl").alias("official_pnl"))
                      .join(flash.select("factor_id", pl.col("pnl").alias("flash_pnl")),
                            on="factor_id"))
        print(comparison)
    except ValueError as e:
        print(f"flash -> refused: {e}")
        print(official)

    print("\ndone.")


if __name__ == "__main__":
    main()
