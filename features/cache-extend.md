# Cache Extend

**Requirement**: CacheBehaviour enum (Strict/Extend, default Extend, settable in the UserCache constructor): under Extend a cache miss is queried once and merged into the working set — coverage becomes per-dataset segments that coalesce and self-heal, and loaders are gap-aware (loader(s, e, assets)) so a partly-covered request fetches only the missing cells on both axes (date gaps AND the book-plus-adds asset gap; missing assets grouped by gap signature, one fetch per group, degrading to one full-request load beyond MAX_FETCH_GROUPS groups) — per Chris's extend-on-demand model (2026-07-20); factor-filtered loads are marked extendable=False so they never poison the all-factors cache shape; selftest demonstrates gap-only fetches on both axes, coalescing, dedupe, the fallback, strict immutability, and the facade-level asset-6 case.

**Started**: 2026-07-20
**Last updated**: 2026-07-20
**Branch**: cache-extend

## Files involved

<!-- populated on commit -->

## History

<!-- populated on commit -->
