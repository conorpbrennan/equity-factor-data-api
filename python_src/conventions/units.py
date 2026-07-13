"""Unit conventions and executable conversions to canonical form.

Canonical (the CANONICAL dict is the single statement of it):
  returns        daily, decimal fraction        0.01  = 1% daily return
  volatility     annualized, decimal            0.15  = 15% annualized vol
  covariance     annualized variance, decimal^2
  money          millions of USD

Returns are daily while risk is annualized by design; convert between the two
with sqrt(TRADING_DAYS). Vendor feeds arrive in their own conventions — the
tags below are the ones model_master carries — and every conversion is a
multiplication, so the whole table reduces to scale_to_canonical().
"""

from __future__ import annotations

TRADING_DAYS = 252

CANONICAL = {
    "return": "daily_dec",
    "specific_risk": "ann_vol_dec",
    "covariance": "ann_var_dec2",
    "money": "usd_mm",
}

# (kind, source convention) -> multiplier into canonical units
_SCALE: dict[tuple[str, str], float] = {
    ("return", "daily_dec"): 1.0,
    ("return", "daily_pct"): 1e-2,
    ("specific_risk", "ann_vol_dec"): 1.0,
    ("specific_risk", "ann_vol_pct"): 1e-2,
    ("specific_risk", "daily_vol"): TRADING_DAYS ** 0.5,
    ("covariance", "ann_var_dec2"): 1.0,
    ("covariance", "ann_var_pct2"): 1e-4,
    ("covariance", "daily_var"): float(TRADING_DAYS),
}


def scale_to_canonical(kind: str, convention: str) -> float:
    """Multiplier taking values in `convention` to canonical units for `kind`.

    >>> scale_to_canonical("specific_risk", "ann_vol_pct")
    0.01
    """
    try:
        return _SCALE[(kind, convention)]
    except KeyError:
        known = sorted(c for k, c in _SCALE if k == kind)
        raise ValueError(
            f"unknown {kind} convention {convention!r}; known: {known}"
        ) from None
