"""Static model inventory — stage 1 of model discoverability.

The importable source of model ids and their descriptors, replacing string
literals scattered through scripts and defaults. Reference the constant
(``inventory.AX_WW4_MH``), never re-type the id.

This file is the *first stage*: a static snapshot of the curated store's
model_master (generated from it — see selftest's drift check). The target
state is the inventory living in the curated model store itself, reflecting
whatever the store's contents are; ``list_models(root)`` already reads that,
and ``refresh(root)`` regenerates this module's table from a live store when
the fleet changes.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelInfo:
    """One model_master row, importable without a store connection."""
    model_id: str
    vendor: str
    model_name: str
    variant: str
    region: str
    n_factors: int
    cov_scaling: str
    specific_risk_convention: str
    return_convention: str
    base_model_id: str | None = None


# --- model id constants: reference these, never re-type the literal --------
AX_WW4_MH = "AX_WW4_MH"
AX_US4_MH = "AX_US4_MH"
AX_EU4_MH = "AX_EU4_MH"
AX_JP4_MH = "AX_JP4_MH"
AX_EM4_MH = "AX_EM4_MH"
AX_WW4_MH_SFM1 = "AX_WW4_MH_SFM1"
BARRA_GEM_L = "BARRA_GEM_L"
BARRA_USE4_L = "BARRA_USE4_L"
BARRA_EUE4_L = "BARRA_EUE4_L"
BARRA_JPE4_L = "BARRA_JPE4_L"
BARRA_EME4_L = "BARRA_EME4_L"
BARRA_USE4_L_SFM1 = "BARRA_USE4_L_SFM1"

INVENTORY: dict[str, ModelInfo] = {m.model_id: m for m in (
    ModelInfo(AX_WW4_MH, "SimCorp Axioma", "WW4", "MH", "US/EU/JP/EM/ROW",
              248, "daily_var", "daily_vol", "daily_dec"),
    ModelInfo(BARRA_GEM_L, "MSCI Barra", "GEM", "L", "US/EU/JP/EM/ROW",
              233, "ann_var_pct2", "ann_vol_pct", "daily_pct"),
    ModelInfo(BARRA_USE4_L, "MSCI Barra", "USE4", "L", "US",
              73, "ann_var_pct2", "ann_vol_pct", "daily_pct"),
    ModelInfo(AX_US4_MH, "SimCorp Axioma", "US4", "MH", "US",
              82, "daily_var", "daily_vol", "daily_dec"),
    ModelInfo(BARRA_EUE4_L, "MSCI Barra", "EUE4", "L", "EU",
              99, "ann_var_pct2", "ann_vol_pct", "daily_pct"),
    ModelInfo(AX_EU4_MH, "SimCorp Axioma", "EU4", "MH", "EU",
              95, "daily_var", "daily_vol", "daily_dec"),
    ModelInfo(BARRA_JPE4_L, "MSCI Barra", "JPE4", "L", "JP",
              43, "ann_var_pct2", "ann_vol_pct", "daily_pct"),
    ModelInfo(AX_JP4_MH, "SimCorp Axioma", "JP4", "MH", "JP",
              47, "daily_var", "daily_vol", "daily_dec"),
    ModelInfo(BARRA_EME4_L, "MSCI Barra", "EME4", "L", "EM",
              115, "ann_var_pct2", "ann_vol_pct", "daily_pct"),
    ModelInfo(AX_EM4_MH, "SimCorp Axioma", "EM4", "MH", "EM",
              122, "daily_var", "daily_vol", "daily_dec"),
    ModelInfo(AX_WW4_MH_SFM1, "SimCorp Axioma", "WW4", "MH",
              "US/EU/JP/EM/ROW", 251, "daily_var", "daily_vol", "daily_dec",
              base_model_id=AX_WW4_MH),
    ModelInfo(BARRA_USE4_L_SFM1, "MSCI Barra", "USE4", "L", "US",
              75, "ann_var_pct2", "ann_vol_pct", "daily_pct",
              base_model_id=BARRA_USE4_L),
)}

# The default model for CLIs and examples: the worldwide Axioma base model.
DEFAULT_MODEL = AX_WW4_MH


def model_ids() -> tuple[str, ...]:
    """Every model id in the inventory, insertion (store) order."""
    return tuple(INVENTORY)


def refresh(root: str | None = None) -> str:
    """Regenerate the INVENTORY literal from a live store's model_master.

    Prints (and returns) the replacement ``INVENTORY`` block for this file
    when the curated fleet changes — the static table should never be
    hand-edited into drift.
    """
    from .store import Store
    rows = Store.open(root).dim("model_master").to_dicts()
    lines = ["INVENTORY: dict[str, ModelInfo] = {m.model_id: m for m in ("]
    for r in rows:
        base = (f",\n              base_model_id={r['base_model_id']!r}"
                if r["base_model_id"] else "")
        lines.append(
            f"    ModelInfo({r['model_id']!r}, {r['vendor']!r}, "
            f"{r['model_name']!r}, {r['variant']!r}, {r['region']!r},\n"
            f"              {r['n_factors']}, {r['cov_scaling']!r}, "
            f"{r['specific_risk_convention']!r}, "
            f"{r['return_convention']!r}{base}),")
    lines.append(")}")
    block = "\n".join(lines)
    print(block)
    return block
