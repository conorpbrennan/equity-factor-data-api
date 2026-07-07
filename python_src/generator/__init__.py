"""Synthetic multi-model equity factor data generator.

Implements generator-spec.md: normalized Parquet store of Barra/Axioma-style
factor model data with AR(1) temporal persistence and a counter-based seed
scheme (same config => byte-identical output).
"""

__version__ = "0.1.0"
