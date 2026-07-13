# Equity Factor Data API — user-layer packages

Generated API reference for the two user-layer packages. The narrative design
docs (why these shapes, contracts, open questions) live alongside in the repo:

- [user-layer.html](https://github.com/conorpbrennan/equity-factor-data-api/blob/main/docs/user-layer.html) — design rationale for both packages
- [v2-report.html](https://github.com/conorpbrennan/equity-factor-data-api/blob/main/docs/v2-report.html) — the v2 factor-store benchmark report

## The two packages

**[conventions](api/conventions.md)** — firm-wide data conventions as an
importable library: shared column constants, executable unit conversions,
typed identifier schemes, canonical argument names, and the adapter toolkit
for legacy data.

**[modelfacade](api/modelfacade.md)** — two-layer model data access: a strict
core `Model` (dates only, internal ids, raw vendor units, fail-fast) and a
lenient user-facing `ModelFacade` (string dates, vendor ids, canonical units,
wide one-liners, pre-warmable persistent cache, T0 estimate stream).

## Quick start

```bash
cd python_src
python -m modelfacade selftest        # 12 contract checks, no data needed
python warm_cache.py --demo           # the morning job, persistent demo
python usage_example.py               # narrated tour, consumes the above
```

```python
from modelfacade import ModelFacade

fac = ModelFacade.load("AX_WW4_MH")               # $FACTOR_STORE_ROOT
df = fac.get_factor_loadings("latest")            # wide, one line
est = fac.get_factor_returns(estimates=True)      # T0 estimate stream
```
