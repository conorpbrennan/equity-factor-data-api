"""Parameter block (generator-spec.md §2) and the two pinned model configs."""

from __future__ import annotations

import dataclasses
import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

BARRA_STYLES = (
    "BETA", "MOMENTUM", "SIZE", "EARNYLD", "RESVOL", "GROWTH",
    "BTOP", "LEVERAGE", "LIQUIDTY", "SIZENL", "DIVYLD", "SENTMT",
)
AXIOMA_STYLES = (
    "MARKET_SENSITIVITY", "MT_MOMENTUM", "ST_MOMENTUM", "SIZE", "VALUE",
    "GROWTH", "LEVERAGE", "LIQUIDITY", "VOLATILITY", "EXCHANGE_RATE_SENS",
    "DIVIDEND_YIELD", "PROFITABILITY", "EARNINGS_YIELD",
)

SECTORS = (
    "ENERGY", "MATERIALS", "INDUSTRIALS", "CONS_DISC", "CONS_STAPLES",
    "HEALTH_CARE", "FINANCIALS", "INFO_TECH", "COMM_SVCS", "UTILITIES",
    "REAL_ESTATE",
)


@dataclass(frozen=True)
class ModelConfig:
    model_id: str
    vendor: str
    model_name: str
    variant: str
    style_factors: tuple[str, ...]
    n_industries: int
    industry_prefix: str
    market_factor: str
    cov_scaling: str                # 'ann_var_pct2' | 'daily_var'
    specific_risk_convention: str   # 'ann_vol_pct'  | 'daily_vol'

    # Dynamics
    style_ar_phi: float = 0.99
    cov_ar_phi: float = 0.997
    srisk_ar_phi: float = 0.995
    industry_switch_annual: float = 0.01
    dual_industry_frac: float = 0.10

    # Covariance structure
    cov_k: int = 10
    vol_market_ann: float = 0.16
    vol_industry_ann: tuple[float, float] = (0.15, 0.35)
    vol_style_ann: tuple[float, float] = (0.02, 0.10)

    # Specific risk (internal units: annualized decimal vol)
    srisk_median_ann: float = 0.28
    srisk_mu_sd: float = 0.25
    srisk_logvol_sd: float = 0.35
    srisk_clip_ann: tuple[float, float] = (0.05, 1.00)

    # Coverage
    coverage_rate: float = 0.985
    estu_rate: float = 0.90

    @property
    def n_styles(self) -> int:
        return len(self.style_factors)

    @property
    def n_factors(self) -> int:
        return self.n_styles + self.n_industries + 1

    @property
    def factor_ids(self) -> list[str]:
        """Factor mnemonics in factor_seq order: styles, industries, market."""
        industries = [f"{self.industry_prefix}{i:02d}" for i in range(1, self.n_industries + 1)]
        return list(self.style_factors) + industries + [self.market_factor]

    @property
    def factor_types(self) -> list[str]:
        return (["STYLE"] * self.n_styles
                + ["INDUSTRY"] * self.n_industries
                + ["MARKET"])


BARRA_USE4_L = ModelConfig(
    model_id="BARRA_USE4_L",
    vendor="MSCI Barra",
    model_name="USE4",
    variant="L",
    style_factors=BARRA_STYLES,
    n_industries=60,
    industry_prefix="IND",
    market_factor="COUNTRY",
    cov_scaling="ann_var_pct2",
    specific_risk_convention="ann_vol_pct",
)

AXIOMA_US4_MH = ModelConfig(
    model_id="AXIOMA_US4_MH",
    vendor="SimCorp Axioma",
    model_name="US4",
    variant="MH",
    style_factors=AXIOMA_STYLES,
    n_industries=68,
    industry_prefix="SEC",
    market_factor="MARKET",
    cov_scaling="daily_var",
    specific_risk_convention="daily_vol",
)


@dataclass(frozen=True)
class GeneratorConfig:
    global_seed: int = 20260707

    # Calendar
    start_date: date = date(2006, 1, 2)
    end_date: date = date(2025, 12, 31)

    # Universe
    n_live: int = 3_000
    n_superset: int = 6_500
    annual_churn: float = 0.05

    # Output
    output_dir: str = "data/normalized"
    checkpoint_dir: str = "data/checkpoints"
    compression: str = "zstd"
    compression_level: int = 3
    row_group_size: int = 4_000_000

    models: tuple[ModelConfig, ...] = (BARRA_USE4_L, AXIOMA_US4_MH)


def load_config(path: str | Path | None = None) -> GeneratorConfig:
    """Defaults, with optional top-level scalar overrides from a TOML file.

    Model definitions are pinned in code (spec: adding a model is a config-code
    change, never a schema change); TOML overrides cover the global block only.
    """
    cfg = GeneratorConfig()
    if path is None:
        return cfg
    data = tomllib.loads(Path(path).read_text())
    valid = {f.name for f in dataclasses.fields(GeneratorConfig)} - {"models"}
    unknown = set(data) - valid
    if unknown:
        raise ValueError(f"unknown config keys: {sorted(unknown)}")
    return dataclasses.replace(cfg, **data)
