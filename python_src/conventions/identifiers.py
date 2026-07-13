"""Security identifier conventions: sec_id + sec_id_type.

There is no assumption of an in-house security master. A dataframe carrying a
single identifier scheme uses (sec_id, sec_id_type); one carrying several uses
explicit columns named by sec_id_col(). The allowed sec_id_type values are the
enum below — shared constants so 'barra' vs 'Barra' vs 'BARRA_ID' can't drift.

Vendor id mapping is many-to-many (one Barra id can map to several Axioma ids
and vice versa) and membership changes over time: mapping tables must be dated,
and vendor->vendor mappings are never chained silently — always via the
internal asset_id.
"""

from __future__ import annotations

from enum import Enum


class SecurityIDType(str, Enum):
    """Values match the vendor column of the asset_xref table."""
    INTERNAL = "INTERNAL"   # asset_id, the only join key inside the store
    BARRA = "BARRA"
    AXIOMA = "AXIOMA"
    TICKER = "TICKER"

    def __str__(self) -> str:           # so f-strings render 'BARRA'
        return self.value


def sec_id_col(id_type: SecurityIDType | str) -> str:
    """Explicit column name for one scheme in a multi-scheme dataframe:
    sec_id_col(SecurityIDType.BARRA) -> 'sec_id_barra'."""
    value = id_type.value if isinstance(id_type, SecurityIDType) else str(id_type)
    return f"sec_id_{value.lower()}"
