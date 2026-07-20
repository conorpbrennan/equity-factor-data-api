# Cache Extend

**Requirement**: CacheBehaviour enum (Strict/Extend, default Extend, settable in the UserCache constructor): under Extend a cache miss is queried once and merged into the working set — coverage becomes per-dataset segments that coalesce and self-heal, and loaders are gap-aware (loader(s, e, assets)) so a partly-covered request fetches only the missing cells on both axes (date gaps AND the book-plus-adds asset gap; missing assets grouped by gap signature, one fetch per group, degrading to one full-request load beyond MAX_FETCH_GROUPS groups) — per Chris's extend-on-demand model (2026-07-20); factor-filtered loads are marked extendable=False so they never poison the all-factors cache shape; selftest demonstrates gap-only fetches on both axes, coalescing, dedupe, the fallback, strict immutability, and the facade-level asset-6 case. Follow-ups on the same branch: stale/missing persisted sets dismissed with re-query instead of raising (Chris 2026-07-20); view identity in the cache key (dataset@view — PIT answers never see later republications, estimates never served as official) with per-cell fresh-wins dedupe.

**Started**: 2026-07-20
**Last updated**: 2026-07-20
**Branch**: cache-extend

## Files involved




- python_src/modelfacade/README.md
- python_src/modelfacade/__init__.py
- python_src/modelfacade/cache.py
- python_src/modelfacade/facade.py
- python_src/modelfacade/selftest.py

## History

- 2026-07-20 `c75532f` — cache: view identity in the cache key + per-cell fresh-wins dedupe
  - python_src/modelfacade/README.md
  - python_src/modelfacade/cache.py
  - python_src/modelfacade/facade.py
  - python_src/modelfacade/selftest.py

- 2026-07-20 `68b8a53` — facade: dismiss stale/missing working sets instead of raising
  - python_src/modelfacade/README.md
  - python_src/modelfacade/facade.py
  - python_src/modelfacade/selftest.py

- 2026-07-20 `09a9f47` — cache: CacheBehaviour Strict/Extend (default Extend) with 2-D fetch-the-gap
  - python_src/modelfacade/README.md
  - python_src/modelfacade/__init__.py
  - python_src/modelfacade/cache.py
  - python_src/modelfacade/facade.py
  - python_src/modelfacade/selftest.py
