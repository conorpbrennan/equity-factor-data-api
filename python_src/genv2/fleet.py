"""V2 fleet configuration (generator-spec-v2.md §1) and global parameters."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

# ---------------------------------------------------------------- universes

# region -> live names on any COB date (spec §1); superset = live * 2.2
REGIONS: dict[str, int] = {
    "US": 3_000, "EU": 6_000, "JP": 4_000, "EM": 8_000, "ROW": 37_000,
}
SUPERSET_FACTOR = 2.2

# Universal geographic code spaces (per-asset, static). A model maps codes
# onto its own country/currency factors modulo its factor count.
N_COUNTRY_CODES, N_CCY_CODES = 95, 72
REGION_COUNTRY_RANGE = {"US": (0, 1), "JP": (1, 2), "EU": (2, 26),
                        "EM": (26, 64), "ROW": (0, 95)}
REGION_CCY_RANGE = {"US": (0, 1), "JP": (1, 2), "EU": (2, 14),
                    "EM": (14, 42), "ROW": (0, 72)}

BARRA_STYLES = ("BETA", "MOMENTUM", "SIZE", "EARNYLD", "RESVOL", "GROWTH",
                "BTOP", "LEVERAGE", "LIQUIDTY", "SIZENL", "DIVYLD", "SENTMT")
AXIOMA_STYLES = ("MARKET_SENSITIVITY", "MT_MOMENTUM", "ST_MOMENTUM", "SIZE",
                 "VALUE", "GROWTH", "LEVERAGE", "LIQUIDITY", "VOLATILITY",
                 "EXCHANGE_RATE_SENS", "DIVIDEND_YIELD", "PROFITABILITY",
                 "EARNINGS_YIELD")
AX_GLOBAL_STYLES = AXIOMA_STYLES + ("CROWDING", "ESG_MOMENTUM", "SHORT_INTEREST")


@dataclass(frozen=True)
class V2Model:
    model_id: str
    vendor: str
    model_name: str
    variant: str
    style_factors: tuple[str, ...]
    n_industries: int
    industry_prefix: str
    market_factor: str
    cov_scaling: str                 # 'ann_var_pct2' | 'daily_var'
    specific_risk_convention: str    # 'ann_vol_pct'  | 'daily_vol'
    return_convention: str           # 'daily_pct'    | 'daily_dec'
    universe_regions: tuple[str, ...]
    coverage_rate: float
    estu_rate: float
    n_countries: int = 0
    n_currencies: int = 0
    # customization variants (spec §1): shared factors byte-identical to base
    base_model_id: str | None = None
    n_base_styles: int = 0

    # dynamics (v1 values)
    style_ar_phi: float = 0.99
    cov_ar_phi: float = 0.997
    srisk_ar_phi: float = 0.995
    industry_switch_annual: float = 0.01
    dual_industry_frac: float = 0.10
    cov_k: int = 10
    vol_market_ann: float = 0.16
    vol_industry_ann: tuple = (0.15, 0.35)
    vol_style_ann: tuple = (0.02, 0.10)
    vol_country_ann: tuple = (0.08, 0.25)
    vol_ccy_ann: tuple = (0.05, 0.15)
    srisk_median_ann: float = 0.28
    srisk_mu_sd: float = 0.25
    srisk_logvol_sd: float = 0.35
    srisk_clip_ann: tuple = (0.05, 1.00)
    # new datasets
    fmp_ar_phi: float = 0.97
    ret_ar_phi: float = 0.10
    # restatements
    restate_rate: float = 0.01
    restate_max_lag: int = 5

    @property
    def n_styles(self) -> int:
        return len(self.style_factors)

    @property
    def n_factors(self) -> int:
        return (self.n_styles + self.n_industries + self.n_countries
                + self.n_currencies + 1)

    @property
    def factor_ids(self) -> list[str]:
        return (list(self.style_factors)
                + [f"{self.industry_prefix}{i:02d}" for i in range(1, self.n_industries + 1)]
                + [f"CTY{i:02d}" for i in range(1, self.n_countries + 1)]
                + [f"CCY{i:02d}" for i in range(1, self.n_currencies + 1)]
                + [self.market_factor])

    @property
    def factor_types(self) -> list[str]:
        return (["STYLE"] * self.n_styles + ["INDUSTRY"] * self.n_industries
                + ["COUNTRY"] * self.n_countries + ["CURRENCY"] * self.n_currencies
                + ["MARKET"])

    @property
    def fmp_factor_seq(self) -> list[int]:
        """FMPs exist for styles + market (spec §2)."""
        return list(range(self.n_styles)) + [self.n_factors - 1]

    def seed_id(self, shared: bool) -> str:
        """Variants draw shared components from the base model's streams."""
        return self.base_model_id if (shared and self.base_model_id) else self.model_id


def _barra(mid, name, styles, n_ind, regions, cov, estu, n_cty=0, n_ccy=0, **kw):
    return V2Model(model_id=mid, vendor="MSCI Barra", model_name=name, variant="L",
                   style_factors=styles, n_industries=n_ind, industry_prefix="IND",
                   market_factor="COUNTRY" if n_cty == 0 else "WORLD",
                   cov_scaling="ann_var_pct2", specific_risk_convention="ann_vol_pct",
                   return_convention="daily_pct", universe_regions=regions,
                   coverage_rate=cov, estu_rate=estu,
                   n_countries=n_cty, n_currencies=n_ccy, **kw)


def _axioma(mid, name, styles, n_ind, regions, cov, estu, n_cty=0, n_ccy=0, **kw):
    return V2Model(model_id=mid, vendor="SimCorp Axioma", model_name=name, variant="MH",
                   style_factors=styles, n_industries=n_ind, industry_prefix="SEC",
                   market_factor="MARKET",
                   cov_scaling="daily_var", specific_risk_convention="daily_vol",
                   return_convention="daily_dec", universe_regions=regions,
                   coverage_rate=cov, estu_rate=estu,
                   n_countries=n_cty, n_currencies=n_ccy, **kw)


ALL_REGIONS = ("US", "EU", "JP", "EM", "ROW")

FLEET: dict[str, V2Model] = {m.model_id: m for m in [
    _axioma("AX_WW4_MH", "WW4", AX_GLOBAL_STYLES, 64, ALL_REGIONS,
            cov=1.0, estu=13_000 / 58_000, n_cty=95, n_ccy=72),
    _barra("BARRA_GEM_L", "GEM", BARRA_STYLES, 60, ALL_REGIONS,
           cov=50_000 / 58_000, estu=11_000 / 50_000, n_cty=90, n_ccy=70),
    _barra("BARRA_USE4_L", "USE4", BARRA_STYLES, 60, ("US",), cov=1.0, estu=0.9),
    _axioma("AX_US4_MH", "US4", AXIOMA_STYLES, 68, ("US",), cov=1.0, estu=0.9),
    _barra("BARRA_EUE4_L", "EUE4", BARRA_STYLES, 50, ("EU",),
           cov=1.0, estu=2_500 / 6_000, n_cty=24, n_ccy=12),
    _axioma("AX_EU4_MH", "EU4", AXIOMA_STYLES, 45, ("EU",),
            cov=1.0, estu=2_500 / 6_000, n_cty=24, n_ccy=12),
    _barra("BARRA_JPE4_L", "JPE4", BARRA_STYLES, 30, ("JP",), cov=1.0, estu=0.45),
    _axioma("AX_JP4_MH", "JP4", AXIOMA_STYLES, 33, ("JP",), cov=1.0, estu=0.45),
    _barra("BARRA_EME4_L", "EME4", BARRA_STYLES, 38, ("EM",),
           cov=1.0, estu=3_000 / 8_000, n_cty=38, n_ccy=26),
    _axioma("AX_EM4_MH", "EM4", AXIOMA_STYLES, 40, ("EM",),
            cov=1.0, estu=3_000 / 8_000, n_cty=40, n_ccy=28),
    # customization variants: new model_id, shared factors byte-identical to base
    _axioma("AX_WW4_MH_SFM1", "WW4", AX_GLOBAL_STYLES + ("SFM_ALPHA1", "SFM_ALPHA2", "SFM_ALPHA3"),
            64, ALL_REGIONS, cov=1.0, estu=13_000 / 58_000, n_cty=95, n_ccy=72,
            base_model_id="AX_WW4_MH", n_base_styles=len(AX_GLOBAL_STYLES)),
    _barra("BARRA_USE4_L_SFM1", "USE4", BARRA_STYLES + ("SFM_QUAL1", "SFM_QUAL2"),
           60, ("US",), cov=1.0, estu=0.9,
           base_model_id="BARRA_USE4_L", n_base_styles=len(BARRA_STYLES)),
    # ---- DRILL additions (2026-07-10): the add-a-model exercise -----------
    _axioma("AX_CA4_MH", "CA4", AXIOMA_STYLES, 28, ("US",),
            cov=0.2, estu=0.6),                      # Canada-like small regional
    _barra("BARRA_EUE4_L_SFM1", "EUE4", BARRA_STYLES + ("SFM_ESG1", "SFM_ESG2"),
           50, ("EU",), cov=1.0, estu=2_500 / 6_000, n_cty=24, n_ccy=12,
           base_model_id="BARRA_EUE4_L", n_base_styles=len(BARRA_STYLES)),
]}

TIERS = {
    "dev": ["BARRA_USE4_L", "AX_US4_MH", "BARRA_JPE4_L", "AX_JP4_MH",
            "BARRA_USE4_L_SFM1"],
    "full": [m for m in FLEET if m not in ("AX_CA4_MH", "BARRA_EUE4_L_SFM1")],
    "drill": ["AX_CA4_MH", "BARRA_EUE4_L_SFM1"],
}


@dataclass(frozen=True)
class V2Config:
    global_seed: int = 20260708
    start_date: date = date(2006, 1, 2)
    end_date: date = date(2025, 12, 31)
    output_dir: str = "data/v2/normalized"
    checkpoint_dir: str = "data/v2/checkpoints"
    compression: str = "zstd"
    compression_level: int = 3
    row_group_size: int = 4_000_000
    # write one file per month once a model exceeds this many loading rows/year
    monthly_chunk_threshold: int = 60_000_000
    models: tuple[V2Model, ...] = ()


def make_config(tier: str, **overrides) -> V2Config:
    models = tuple(FLEET[mid] for mid in TIERS[tier])
    return V2Config(models=models, **overrides)
