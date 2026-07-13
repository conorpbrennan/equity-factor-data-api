# modelfacade — strict core, lenient user layer

## Why this project

Core model libraries are (rightly) strict: exact date types, native
identifiers, validated inputs, always-right-or-fails. End users need the
opposite: string dates, whatever identifier scheme their positions carry,
one-liners, discoverability, and caching they control. Serving both from one
class satisfies neither — the resolution is two layers with two contracts and
explicit conversion between them.

## Proposed change

`Model` (core.py) — the strict layer, shaped like a core computation library:

- `datetime.date` only (rejects strings *and* datetimes: a COB has no time)
- internal integer asset ids only; factor ids validated against the master
- values raw, exactly as the vendor published them; conventions exposed as
  metadata, never silently applied

`ModelFacade` (facade.py) — a lenient wrapper holding a core `Model`:

- dates as `date`, `datetime`, `'YYYY-MM-DD'`, or `'latest'`
- assets as internal ints or vendor ids (resolved via `asset_xref`, scheme
  auto-detected or pinned with `sec_id_type=`)
- outputs in canonical units (`conventions.units`), whatever the vendor stored
- wide one-liners: `ModelFacade.load(mid).get_factor_loadings("latest")`
- output in the user's dataframe library: `output="polars"` (default here) or
  `output="pandas"` — internals stay polars/Arrow, conversion happens once at
  the return boundary
- discoverability: `list_models()`, `.factors`, `.styles`, `.describe()`
- a user cache designed around **pre-warming an expected working set**
  (`warm(assets)` = YTD loadings + specific risk for a position list, plus all
  factor returns), serving subset requests from it — not query-result caching
- working-set **persistence**: `save_cache()` / `load_cache()` write the
  warmed frames as parquet + a coverage manifest, keyed by
  (as-of date, model_id) under `<temp>/usercache/<as_of>/<model_id>/`
  (`$FACTOR_CACHE_DIR` overrides the base), so one scheduled warm serves
  later sessions; `load_cache()` defaults to the newest date for its model
  and refuses a set saved for a different model

Composition keeps the layers honest: `ModelFacade(model)` wraps, `.core`
unwraps, and user-cache leniency cannot leak into core computations.

## What the scaffold shows

- `python -m modelfacade selftest` — fabricates a micro store (genv2 layout)
  in a temp dir and checks every contract above, one PASS line each.
- `python -m modelfacade demo --root DIR` — the one-liner usage story against
  a real v2 store (12-model fleet, local or s3://).
- `python warm_cache.py --demo` (python_src/) — the scheduled morning job:
  positions file in, one persisted working set per model out; cron-ready.
- Facade reads the normalized store and pivots for wide output — correct
  anywhere, slow at global scale by design of the benchmark findings; wiring
  the generic-slot fast path (`Store.generic_cs_glob`) into
  `get_factor_loadings` is the natural next step.

## Open questions

- Which dataframe library does the user layer speak? The installed base of
  risk notebooks and the existing core library are pandas-native (dates as
  `pd.Timestamp`, "pandas user cache"), which argues for `output="pandas"` as
  the deployed default; this repo defaults to polars to match its own stack.
  The mechanism makes it a per-facade setting either way — the real decision
  is only the default, plus whether pandas becomes a hard dependency.
- Ownership: does the facade live with the core library team (facade and core
  must stay in sync) or with the team that owns application-to-portfolio use?
  Code location next to the core either way.
- Units at which layer: core-raw / facade-canonical (as built) means two
  callers can hold different numbers for the same quantity; core-canonical
  would push vendor conventions down into ingest instead.
- Cache invalidation: the pre-warm design sidesteps TTLs (a working set is
  explicitly re-warmed), but restatements mean a warmed frame can go stale
  within a day — is re-warm-on-publication enough?
- Point-in-time: `version` is plumbed through the core layer; the facade
  doesn't yet expose an `as_known_on=` overlay (the store supports it — see
  the v2 report's PIT section).
