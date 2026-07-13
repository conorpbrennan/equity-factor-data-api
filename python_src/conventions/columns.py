"""Canonical column names as shared string constants, plus the adapter toolkit.

Rule: code references the constant, never re-types the literal. The value of
the case convention is consistency, not the specific choice — these are
snake_case pending ratification.
"""

from __future__ import annotations

import re

# ------------------------------------------------------------- canonical names
MODEL_ID = "model_id"
COB_DATE = "cob_date"          # close-of-business date, always a DATE
ASSET_ID = "asset_id"          # internal integer id
SEC_ID = "sec_id"              # external identifier, typed by SEC_ID_TYPE
SEC_ID_TYPE = "sec_id_type"
FACTOR_ID = "factor_id"
FACTOR_SEQ = "factor_seq"
FACTOR_NAME = "factor_name"
FACTOR_TYPE = "factor_type"    # STYLE | INDUSTRY | COUNTRY | CURRENCY | MARKET
VALUE = "value"
VERSION_ID = "version_id"      # 1 = original publication; >1 = restatement
WEIGHT = "weight"
RETURN = "return"              # daily, decimal fraction (units.CANONICAL)
SPECIFIC_RISK = "specific_risk"


# ------------------------------------------------------------- adapter toolkit
def snake_case(name: str) -> str:
    """Any naming style -> snake_case: 'CobDate' / 'cob date' / 'COB-DATE'
    all become 'cob_date'. Runs of caps stay one word (ASOFDATE -> asofdate).
    """
    s = re.sub(r"[\s\-.]+", "_", str(name).strip())
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", s)
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", s)
    return re.sub(r"__+", "_", s).lower()


def rename_snake(df):
    """Return df with every column renamed to snake_case (polars or pandas)."""
    mapping = {c: snake_case(c) for c in df.columns}
    if type(df).__module__.split(".")[0] == "pandas":
        return df.rename(columns=mapping)
    return df.rename(mapping)
