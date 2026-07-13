"""Firm-wide data conventions (roadmap project 1) as an importable library.

The convention is only real if code can import it: canonical column names are
shared string constants, unit conventions are executable conversions, and the
adapter toolkit (snake_case et al.) handles systems that don't comply yet.
Everything new in this repo imports from here rather than re-typing literals.

Canonical choices (each is a straw man pending ratification — see README.md):
  columns      snake_case everywhere; one shared constant per column name
  identifiers  sec_id + sec_id_type; explicit columns when multi-schema
  units        returns daily decimal; vol annualized decimal;
               covariance annualized variance (decimal^2); money millions USD
  signatures   as_of / start / end / assets / factors / model
"""

from .columns import (
    ASSET_ID, COB_DATE, FACTOR_ID, FACTOR_NAME, FACTOR_SEQ, FACTOR_TYPE,
    MODEL_ID, RETURN, SEC_ID, SEC_ID_TYPE, SPECIFIC_RISK, VALUE, VERSION_ID,
    WEIGHT, rename_snake, snake_case,
)
from .identifiers import SecurityIDType, sec_id_col
from .units import CANONICAL, TRADING_DAYS, scale_to_canonical
from .signatures import CANONICAL_PARAMS

__all__ = [
    "ASSET_ID", "COB_DATE", "FACTOR_ID", "FACTOR_NAME", "FACTOR_SEQ",
    "FACTOR_TYPE", "MODEL_ID", "RETURN", "SEC_ID", "SEC_ID_TYPE",
    "SPECIFIC_RISK", "VALUE", "VERSION_ID", "WEIGHT",
    "rename_snake", "snake_case",
    "SecurityIDType", "sec_id_col",
    "CANONICAL", "TRADING_DAYS", "scale_to_canonical",
    "CANONICAL_PARAMS",
]
