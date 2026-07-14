"""Security identifier conventions: sec_id + sec_id_type.

There is no assumption of an in-house security master. A dataframe carrying a
single identifier scheme uses (sec_id, sec_id_type); one carrying several uses
explicit columns named by sec_id_col(). sec_id_type is a plain string — any
scheme name is allowed, so an odd one-off never needs a code change — with the
usual schemes as shared constants so 'barra' vs 'Barra' vs 'BARRA_ID' can't
drift in code. Comparisons are case-insensitive: normalize with
sec_id_type_str() at the boundary.

Vendor id mapping is many-to-many (one Barra id can map to several Axioma ids
and vice versa) and membership changes over time: mapping tables must be dated,
and vendor->vendor mappings are never chained silently — always via the
internal asset_id.
"""

from __future__ import annotations


class SecurityIDType:
    """Known scheme names, as plain strings (values match the vendor column
    of the asset_xref table). Not an enum on purpose: any string is a valid
    scheme, these constants just keep the usual ones typo-proof."""
    INTERNAL = "INTERNAL"   # asset_id, the only join key inside the store
    BARRA = "BARRA"
    AXIOMA = "AXIOMA"
    TICKER = "TICKER"


def sec_id_type_str(id_type: str) -> str:
    """Canonical form of a scheme name: the uppercased string.
    sec_id_type_str('barra') -> 'BARRA'; unknown schemes pass through."""
    return str(id_type).upper()


def sec_id_col(id_type: str) -> str:
    """Explicit column name for one scheme in a multi-scheme dataframe:
    sec_id_col(SecurityIDType.BARRA) -> 'sec_id_barra'."""
    return f"sec_id_{str(id_type).lower()}"
