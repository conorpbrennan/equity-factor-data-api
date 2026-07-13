"""End-to-end selftest against a fabricated micro store (no real data needed).

What it does, in order:

  1. Builds a complete two-model store in a temp dir, byte-for-byte in the
     genv2 layout (dimension parquet at the root, hive-partitioned facts
     under model_id=<M>/year=<Y>/) — one tiny Axioma-convention model with
     full facts, one Barra-convention model as dimensions only.
  2. Runs one check function per contract of the three packages under test
     (conventions, core Model, ModelFacade), each opening the store exactly
     as a user would.
  3. Prints one PASS/FAIL line per check and exits nonzero on any failure.

Every fact value in the micro store is deterministic (loading_value below),
so checks can assert exact numbers — including that unit conversions applied
the right multiplier and that a wide pivot filled the right cells.

    python -m modelfacade selftest
"""

from __future__ import annotations

import os
import shutil
import tempfile
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

from conventions import SecurityIDType, scale_to_canonical, snake_case
from conventions.signatures import DISCOURAGED

from .core import Model
from .facade import ModelFacade
from .store import Store, list_models

# The micro fleet: MID gets full facts; the Barra model exists only in the
# dimension tables so list_models() has a second row and a second set of
# vendor unit conventions to describe.
MID = "AX_TEST1_MH"
STYLES = ("MARKET_SENSITIVITY", "MT_MOMENTUM", "VALUE")
FACTORS = STYLES + ("IND01", "IND02", "MARKET")
ASSETS = list(range(1, 7))


def _weekdays(start: date, n: int) -> list[date]:
    """First n weekdays from `start` — the micro store's trading calendar."""
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


DATES = _weekdays(date(2025, 1, 2), 10)          # 2025-01-02 .. 2025-01-15


def loading_value(asset: int, seq: int, day: int) -> float:
    """Deterministic style loading for (asset, factor_seq, day index) —
    checks recompute this to assert the store round-trips values exactly."""
    return round(0.1 * asset + 0.01 * seq + 0.001 * day, 6)


def build_micro_store(root: Path) -> None:
    """Fabricate the store: 4 dimension tables + 5 fact tables for MID.

    Follows generator-spec-v2's normalized layout so Store/Model/ModelFacade
    read it with the exact same globs they would use on a real v2 store.
    """
    norm = root / "normalized"
    norm.mkdir(parents=True)

    # -- model_master: two models with *different* vendor unit conventions,
    #    so the facade's canonicalization has something real to key off.
    pq.write_table(pa.table({
        "model_id": [MID, "BARRA_TEST1_L"],
        "vendor": ["SimCorp Axioma", "MSCI Barra"],
        "model_name": ["TEST1", "TEST1"], "variant": ["MH", "L"],
        "region": ["US", "US"],
        "n_factors": pa.array([len(FACTORS), 2], type=pa.int16()),
        "cov_scaling": ["ann_var_pct2", "daily_var"],
        "specific_risk_convention": ["ann_vol_pct", "daily_vol"],
        "return_convention": ["daily_pct", "daily_dec"],
        "base_model_id": [None, None],
    }), norm / "model_master.parquet")

    # -- factor_master: MID's 6 factors in seq order (3 styles, 2 industries,
    #    market) — the core layer validates every factors= argument against
    #    this, and the facade derives .styles / wide column order from it.
    types = ["STYLE"] * 3 + ["INDUSTRY"] * 2 + ["MARKET"]
    pq.write_table(pa.table({
        "model_id": [MID] * 6 + ["BARRA_TEST1_L"] * 2,
        "factor_id": list(FACTORS) + ["BETA", "MARKET"],
        "factor_seq": pa.array(list(range(6)) + [0, 1], type=pa.int16()),
        "factor_name": list(FACTORS) + ["BETA", "MARKET"],
        "factor_type": types + ["STYLE", "MARKET"],
    }), norm / "factor_master.parquet")

    # -- asset_master: 6 assets, live since 2006, no delistings.
    pq.write_table(pa.table({
        "asset_id": pa.array(ASSETS, type=pa.int32()),
        "ticker": [f"T{a:05d}" for a in ASSETS],
        "sector": ["Tech"] * 6, "region": ["US"] * 6,
        "country_code": pa.array([0] * 6, type=pa.int16()),
        "ccy_code": pa.array([0] * 6, type=pa.int16()),
        "start_date": [date(2006, 1, 2)] * 6,
        "end_date": pa.array([None] * 6, type=pa.date32()),
    }), norm / "asset_master.parquet")

    # -- asset_xref: two vendor ids per asset (B0000001 / AX0000001 style) —
    #    what the facade resolves user-supplied vendor ids through.
    pq.write_table(pa.table({
        "asset_id": pa.array([a for a in ASSETS for _ in range(2)], type=pa.int32()),
        "vendor": ["BARRA", "AXIOMA"] * 6,
        "vendor_asset_id": [v for a in ASSETS
                            for v in (f"B{a:07d}", f"AX{a:07d}")],
    }), norm / "asset_xref.parquet")

    def fact_dir(fact: str) -> Path:
        """Hive partition dir for one fact table: fact/model_id=MID/year=2025."""
        d = norm / fact / f"model_id={MID}" / "year=2025"
        d.mkdir(parents=True)
        return d

    # -- factor_loading: SPARSE long form, like the real store — each
    #    asset-day emits only its nonzero rows: 3 styles + 1 industry one-hot
    #    (odd assets are IND01, even are IND02) + market = 5 rows.
    #    Style values come from loading_value(); one-hots are 1.0.
    rows: dict[str, list] = {"cob_date": [], "asset_id": [], "factor_id": [],
                             "value": [], "version_id": []}
    for di, d in enumerate(DATES):
        for a in ASSETS:
            fids = list(STYLES) + [("IND01" if a % 2 else "IND02"), "MARKET"]
            for f in fids:
                seq = FACTORS.index(f)
                rows["cob_date"].append(d)
                rows["asset_id"].append(a)
                rows["factor_id"].append(f)
                # one-hots are exactly IND* and MARKET — careful: a
                # startswith("MARKET") test would also catch the
                # MARKET_SENSITIVITY style
                one_hot = f == "MARKET" or f.startswith("IND")
                rows["value"].append(1.0 if one_hot
                                     else loading_value(a, seq, di))
                rows["version_id"].append(1)
    pq.write_table(pa.table({
        "cob_date": rows["cob_date"],
        "asset_id": pa.array(rows["asset_id"], type=pa.int32()),
        "factor_id": rows["factor_id"], "value": rows["value"],
        "version_id": pa.array(rows["version_id"], type=pa.int16()),
    }), fact_dir("factor_loading") / "data_00.parquet")

    # -- specific_risk: 28.0 + asset_id, stored in ann_vol_pct per
    #    model_master — so asset 1 must come back 0.29 after canonicalization.
    pq.write_table(pa.table({
        "cob_date": [d for d in DATES for _ in ASSETS],
        "asset_id": pa.array(ASSETS * len(DATES), type=pa.int32()),
        "value": [28.0 + a for _ in DATES for a in ASSETS],   # ann_vol_pct
        "version_id": pa.array([1] * len(DATES) * 6, type=pa.int16()),
    }), fact_dir("specific_risk") / "data_00.parquet")

    # -- factor_return: flat 0.5 in daily_pct — canonical must be 0.005.
    pq.write_table(pa.table({
        "cob_date": [d for d in DATES for _ in FACTORS],
        "factor_id": list(FACTORS) * len(DATES),
        "value": [0.5] * len(DATES) * len(FACTORS),           # daily_pct
        "version_id": pa.array([1] * len(DATES) * len(FACTORS), type=pa.int16()),
    }), fact_dir("factor_return") / "data_00.parquet")

    # -- factor_covariance: upper triangle only (like the real store),
    #    4.0 on the diagonal / 1.0 off, in ann_var_pct2 — diag must
    #    canonicalize to 4e-4.
    pairs = [(f1, f2) for i, f1 in enumerate(FACTORS) for f2 in FACTORS[i:]]
    pq.write_table(pa.table({
        "cob_date": [d for d in DATES for _ in pairs],
        "factor_id_1": [p[0] for _ in DATES for p in pairs],
        "factor_id_2": [p[1] for _ in DATES for p in pairs],
        "value": [4.0 if p[0] == p[1] else 1.0                # ann_var_pct2
                  for _ in DATES for p in pairs],
        "version_id": pa.array([1] * len(DATES) * len(pairs), type=pa.int16()),
    }), fact_dir("factor_covariance") / "data_00.parquet")

    # -- fmp: equal 1/6 weights for the style factors only, every asset-day.
    pq.write_table(pa.table({
        "cob_date": [d for d in DATES for _ in STYLES for _a in ASSETS],
        "factor_id": [f for _ in DATES for f in STYLES for _a in ASSETS],
        "asset_id": pa.array([a for _ in DATES for _f in STYLES
                              for a in ASSETS], type=pa.int32()),
        "weight": [1 / 6] * len(DATES) * len(STYLES) * 6,
        "version_id": pa.array([1] * len(DATES) * len(STYLES) * 6,
                               type=pa.int16()),
    }), fact_dir("fmp") / "data_00.parquet")


# Persistent copy of the micro store for the demo clients: warm_cache.py
# --demo produces a working set from it that usage_example.py then consumes.
# Lives under the repo's gitignored data/ dir; built on first use.
DEMO_STORE_DIR = Path(__file__).resolve().parents[2] / "data" / "demo" / "microstore"


def ensure_micro_store(path: str | Path | None = None) -> Path:
    """Build the micro store at a persistent location if it isn't already
    there (default: <repo>/data/demo/microstore). Idempotent; a partial or
    stale build is cleared and rebuilt."""
    p = Path(path).expanduser() if path else DEMO_STORE_DIR
    if not (p / "normalized" / "model_master.parquet").exists():
        shutil.rmtree(p, ignore_errors=True)   # clear partial builds
        build_micro_store(p)
    return p


# --------------------------------------------------------------------- checks
# Each check opens the store fresh, exactly as a user would, and asserts one
# contract. A raised AssertionError (or any exception) marks the check FAIL.

def check_conventions(_root):
    """conventions package alone: the adapter toolkit normalizes any naming
    style to snake_case, unit conversions are the exact multipliers from the
    straw-man table, unknown conventions are rejected loudly, and the
    discouraged-spelling map points at the canonical name."""
    assert snake_case("CobDate") == "cob_date"        # PascalCase
    assert snake_case("COB-DATE") == "cob_date"       # upper + hyphen
    assert snake_case("factor Group") == "factor_group"   # spaces
    assert snake_case("ASOFDATE") == "asofdate"       # all-caps run = one word
    assert scale_to_canonical("specific_risk", "ann_vol_pct") == 0.01
    assert scale_to_canonical("covariance", "daily_var") == 252.0
    assert DISCOURAGED["asof"] == "as_of"
    try:
        scale_to_canonical("return", "weekly_bps")    # not a known convention
        raise AssertionError("unknown convention accepted")
    except ValueError:
        pass


def check_list_models(root):
    """Store discoverability: list_models() reads model_master and sees the
    whole fleet — including the dims-only Barra model with no facts."""
    models = list_models(str(root))
    assert models.height == 2, models
    assert set(models["model_id"]) == {MID, "BARRA_TEST1_L"}


def check_core_strictness(root):
    """Core contract: fail fast, never coerce. String dates, datetimes (a COB
    has no time), and ints are all rejected with TypeError; unknown models
    and unknown factor ids are rejected with ValueError."""
    model = Model(Store.open(str(root)), MID)
    for bad in ("2025-01-15", datetime(2025, 1, 15), 20250115):
        try:
            model.factor_loadings(bad)
            raise AssertionError(f"core accepted {bad!r}")
        except TypeError:
            pass
    try:
        Model(Store.open(str(root)), "NOPE")
        raise AssertionError("unknown model accepted")
    except ValueError:
        pass
    try:
        model.factor_loadings(DATES[-1], factors=["NOT_A_FACTOR"])
        raise AssertionError("unknown factor accepted")
    except ValueError:
        pass


def check_core_loadings(root):
    """Core reads: long-form loadings stay sparse (exactly the 5 nonzero rows
    per asset-day that were written), values arrive in raw vendor units with
    the conventions exposed as metadata, and dates() spans the store."""
    model = Model(Store.open(str(root)), MID)
    long = model.factor_loadings(DATES[-1])
    assert long.height == 6 * 5, long.height        # sparse: 5 rows per asset
    assert set(long.columns) == {"cob_date", "asset_id", "factor_id",
                                 "value", "version_id"}
    assert model.conventions["return_convention"] == "daily_pct"
    lo, hi = model.dates()
    assert (lo, hi) == (DATES[0], DATES[-1])


def check_facade_dates_and_wide(root):
    """Facade leniency, date edition: 'YYYY-MM-DD' strings resolve to the same
    frame as datetime.date, 'latest' resolves to the store's last COB, and
    wide output pivots sparse long rows into one column per factor in
    factor_seq order — absent one-hots filled with 0.0, style cells matching
    the deterministic generator values exactly."""
    fac = ModelFacade.load(MID, str(root))
    by_str = fac.get_factor_loadings("2025-01-06")
    by_date = fac.get_factor_loadings(date(2025, 1, 6))
    assert by_str.equals(by_date)
    wide = fac.get_factor_loadings("latest")
    assert wide.height == 6                          # one row per asset
    assert wide.columns == ["cob_date", "asset_id", *FACTORS]
    assert wide["asset_id"].dtype == pl.Int32        # 0.0 fill must not upcast
    row2 = wide.filter(pl.col("asset_id") == 2)      # asset 2 is even -> IND02
    assert row2["IND01"][0] == 0.0 and row2["IND02"][0] == 1.0   # one-hot fill
    assert row2["MT_MOMENTUM"][0] == loading_value(2, 1, len(DATES) - 1)
    # MARKET_SENSITIVITY is a style (a value), not the MARKET one-hot (1.0)
    assert row2["MARKET_SENSITIVITY"][0] == loading_value(2, 0, len(DATES) - 1)
    assert row2["MARKET"][0] == 1.0


def check_facade_identifiers(root):
    """Facade leniency, identifier edition: vendor security ids resolve to
    internal asset_ids via asset_xref — with the scheme pinned by
    sec_id_type=, or auto-detected when the ids are unambiguous — and an
    unknown id fails loudly instead of returning an empty frame."""
    fac = ModelFacade.load(MID, str(root))
    wide = fac.get_factor_loadings("latest", assets=["AX0000003", "AX0000005"],
                                   sec_id_type=SecurityIDType.AXIOMA)
    assert sorted(wide["asset_id"]) == [3, 5]
    auto = fac.get_factor_loadings("latest", assets=["B0000004"])  # detected
    assert auto["asset_id"].to_list() == [4]
    try:
        fac.get_factor_loadings("latest", assets=["AX9999999"])
        raise AssertionError("unknown vendor id accepted")
    except ValueError:
        pass


def check_facade_units(root):
    """The two-layer units contract: the facade converts every quantity to
    canonical units using this model's model_master conventions (29.0
    ann_vol_pct -> 0.29; 0.5 daily_pct -> 0.005; 4.0 ann_var_pct2 -> 4e-4)
    while the core hands back the raw stored numbers untouched."""
    fac = ModelFacade.load(MID, str(root))
    srisk = fac.get_specific_risk("latest", assets=[1])
    assert abs(srisk["value"][0] - 0.29) < 1e-12          # 29.0 ann_vol_pct
    raw = fac.core.specific_risk(DATES[-1], assets=[1])
    assert raw["value"][0] == 29.0                        # core stays raw
    rets = fac.get_factor_returns("2025-01-02", "2025-01-15")
    assert abs(rets["value"][0] - 0.005) < 1e-12          # 0.5 daily_pct
    cov = fac.get_covariance("latest")
    diag = cov.filter(pl.col("factor_id_1") == pl.col("factor_id_2"))
    assert abs(diag["value"][0] - 4e-4) < 1e-15           # 4.0 ann_var_pct2


def check_cache_prewarm(root):
    """The pre-warm working-set design: before warm() every request is a
    miss; warm([1,2,3]) loads YTD loadings + specific risk for those assets
    plus all factor returns; afterwards any request *covered* by that set
    (subset assets, subset dates) is served from cache, and a request outside
    the warmed scope (asset 6) falls through to the store and still returns
    correct data."""
    fac = ModelFacade.load(MID, str(root))
    fac.get_factor_loadings("latest", assets=[1])              # cold: miss
    assert fac.cache.stats["misses"] == 1 and fac.cache.stats["hits"] == 0
    fac.warm([1, 2, 3])
    fac.get_factor_loadings("2025-01-10", assets=[1, 2])       # covered
    fac.get_specific_risk("2025-01-08", assets=[3])            # covered
    fac.get_factor_returns("2025-01-02", "2025-01-15")         # covered (all)
    assert fac.cache.stats["hits"] == 3, fac.cache.stats
    out = fac.get_factor_loadings("latest", assets=[6])        # outside scope
    assert out["asset_id"].to_list() == [6]                    # ...but correct
    assert fac.cache.stats["misses"] == 2


def check_cache_persistence(root):
    """Cross-session reuse with the keyed layout: the cache key is
    (as-of COB date, model_id) -> <base>/usercache/<as_of>/<model_id>/.
    Session A warms and save_cache()s; session B (a fresh facade, empty
    cache) load_cache()s with no arguments — resolving the newest date for
    its model — and its first covered request is a hit. The reloaded frames
    are byte-identical, saving with nothing warmed fails, and a facade for
    a different model refuses the saved set."""
    os.environ["FACTOR_CACHE_DIR"] = str(Path(root) / "cachebase")
    try:
        a = ModelFacade.load(MID, str(root))
        try:
            a.save_cache()                              # nothing warmed yet
            raise AssertionError("saved an empty working set")
        except ValueError:
            pass
        a.warm([1, 2, 3])
        saved_to = a.save_cache()
        # the key: as-of date (coverage end = last COB), then model id
        assert saved_to == (Path(root) / "cachebase" / "usercache"
                            / DATES[-1].isoformat() / MID), saved_to
        assert (saved_to / "manifest.json").exists()
        assert (saved_to / "factor_loading.parquet").exists()

        b = ModelFacade.load(MID, str(root))            # 'next session'
        b.load_cache()                                  # newest date, no args
        covered = b.get_factor_loadings("2025-01-10", assets=[1, 2])
        assert b.cache.stats["hits"] == 1 and b.cache.stats["misses"] == 0
        assert covered.height == 2
        assert b.cache.frames["factor_loading"].equals(
            a.cache.frames["factor_loading"])           # lossless round trip
        b.load_cache(as_of=DATES[-1])                   # pinned date works too

        other = ModelFacade.load("BARRA_TEST1_L", str(root))
        try:
            other.load_cache()                          # no set for its model
            raise AssertionError("found a set for a model never saved")
        except FileNotFoundError:
            pass
        try:
            other.load_cache(path=saved_to)             # explicit wrong set
            raise AssertionError("cache for another model accepted")
        except ValueError as e:
            assert MID in str(e)
    finally:
        del os.environ["FACTOR_CACHE_DIR"]


def check_pandas_output(root):
    """The output= option: invalid values rejected at construction; with
    output='pandas' the conversion happens once at the return boundary —
    meaning a helpful install hint when pandas is missing, and genuine
    pd.DataFrames (units already canonical) when it's present. The default
    facade keeps speaking polars either way."""
    try:
        ModelFacade.load(MID, str(root), output="arrow")
        raise AssertionError("bad output value accepted")
    except ValueError:
        pass
    fac = ModelFacade.load(MID, str(root), output="pandas")
    try:
        import pandas as pd
    except ModuleNotFoundError:
        # without pandas installed, the facade must fail with the helpful
        # message at the return boundary, not deep inside polars
        try:
            fac.get_factor_loadings("latest")
            raise AssertionError("pandas output without pandas installed")
        except ModuleNotFoundError as e:
            assert "pip install pandas" in str(e)
        return
    wide = fac.get_factor_loadings("latest")
    assert isinstance(wide, pd.DataFrame) and len(wide) == 6
    srisk = fac.get_specific_risk("latest", assets=[1])
    assert isinstance(srisk, pd.DataFrame)
    assert abs(srisk["value"].iloc[0] - 0.29) < 1e-12   # units survive
    # default facade still speaks polars
    assert isinstance(ModelFacade.load(MID, str(root))
                      .get_factor_loadings("latest"), pl.DataFrame)


def check_layers_interop(root):
    """The two layers convert both ways without loss: facade.core hands back
    the strict Model for core computations, ModelFacade(core) re-wraps it,
    and describe() surfaces the metadata a user needs to orient (styles,
    factor count, raw vendor conventions)."""
    fac = ModelFacade.load(MID, str(root))
    core = fac.core
    assert isinstance(core, Model)
    rewrapped = ModelFacade(core)
    assert rewrapped.model_id == MID
    d = fac.describe()
    assert d["styles"] == list(STYLES) and d["n_factors"] == 6
    assert d["raw_units"]["specific_risk_convention"] == "ann_vol_pct"


CHECKS = [
    ("conventions: snake_case, unit scales, discouraged names", check_conventions),
    ("store: list_models sees the fleet", check_list_models),
    ("core: rejects string/datetime dates, unknown models/factors", check_core_strictness),
    ("core: sparse long loadings, raw conventions, date range", check_core_loadings),
    ("facade: string dates + 'latest', wide pivot with 0.0 fill", check_facade_dates_and_wide),
    ("facade: vendor ids via asset_xref (explicit + detected)", check_facade_identifiers),
    ("facade: canonical units (srisk, returns, covariance)", check_facade_units),
    ("facade: pre-warm cache serves covered subsets", check_cache_prewarm),
    ("facade: cache persists to parquet, reloads across sessions", check_cache_persistence),
    ("facade: output='pandas' at the return boundary", check_pandas_output),
    ("layers: wrap/unwrap round trip, describe()", check_layers_interop),
]


def main() -> int:
    """Build the micro store once, run every check against it, report."""
    failed = 0
    with tempfile.TemporaryDirectory(prefix="modelfacade_selftest_") as tmp:
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
