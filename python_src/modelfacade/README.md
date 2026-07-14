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
- **T0 publication streams**: factor returns carry `type = OFFICIAL |
  T0_ESTIMATE` (orthogonal to `version_id`, which handles restatements);
  `get_factor_returns(estimates=True)` toggles streams via an equality
  filter — never a join — and bypasses the cache. Stores predating the
  column serve official and refuse estimate requests loudly.
- wide one-liners: `ModelFacade.load(mid).get_factor_loadings("latest")`
- output in the user's dataframe library: `output="pandas"` (the default —
  the notebooks this serves are pandas-native) or `output="polars"` —
  internals stay polars/Arrow, conversion happens once at
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
- **invalidation**: saved sets carry a 1-day TTL (the pattern is a morning
  re-warm; pass `max_age_days=None` to knowingly accept an older set), and
  `cache.clear()` drops the whole working set on a known restatement —
  every request then falls through to the store until the next `warm()`
- **offline cold start**: `from_cache(model_id, root)` rebuilds a session
  from a saved set (dims included) with zero store contact; `'latest'`
  freezes at the set's as-of date

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

- ~~Which dataframe library does the user layer speak?~~ Resolved: pandas
  by default — the installed base of risk notebooks is pandas-native and the
  project bias is to change as little as possible. `output="polars"` stays a
  per-facade opt-in; internals are polars/Arrow throughout either way.
- Ownership: does the facade live with the core library team (facade and core
  must stay in sync) or with the team that owns application-to-portfolio use?
  Code location next to the core either way.
- Units at which layer: core-raw / facade-canonical (as built) means two
  callers can hold different numbers for the same quantity; core-canonical
  would push vendor conventions down into ingest instead.
- ~~Cache invalidation~~ Resolved: persisted sets carry a 1-day TTL
  (morning re-warm cadence) and `cache.clear()` handles known restatements;
  users bypass the cache when a question needs fresher data.
- Point-in-time: `version` is plumbed through the core layer; the facade
  doesn't yet expose an `as_known_on=` overlay (the store supports it — see
  the v2 report's PIT section).
