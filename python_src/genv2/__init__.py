"""V2 generator (generator-spec-v2.md): 12-model fleet, multi-region universes,
country/currency factors, factor returns, FMPs, custom variants, restatements.

Deliberately a separate package: the v1 `generator` package is frozen — its
outputs back published benchmarks and the public demo. genv2 reuses v1's
low-level pieces (rng, calendar, parquet writer) unchanged.
"""

__version__ = "2.0.0"
