"""Per-model static draws, evolving state, and per-date emission
(generator-spec.md §4.3–§4.8).

All state arrays span the full slot superset; dead/never-entered slots evolve
too (vectorized, and it makes streams independent of the live set). Values are
emitted only for covered live assets.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .config import GeneratorConfig, ModelConfig
from .rng import round_sig, stream
from .trading_calendar import BUSDAYS_PER_YEAR

DUAL_PRIMARY_WEIGHT = 0.65
DUAL_SECONDARY_WEIGHT = 0.35
COV_DIAG_JITTER = 0.1


@dataclass(frozen=True)
class ModelStatic:
    covered: np.ndarray      # (S,) bool
    estu: np.ndarray         # (S,) bool, subset of covered
    dual: np.ndarray         # (S,) bool — loads on 2 industries
    primary0: np.ndarray     # (S,) int16 — initial industry assignment
    secondary0: np.ndarray   # (S,) int16
    vols: np.ndarray         # (F,) annualized vol targets in factor_seq order
    mu_lnsig: np.ndarray     # (S,) per-asset mean log specific vol
    triu: tuple[np.ndarray, np.ndarray]  # upper-triangle indices (F, F)


def build_static(cfg: GeneratorConfig, m: ModelConfig) -> ModelStatic:
    S = cfg.n_superset
    n_ind = m.n_industries

    g = stream(cfg.global_seed, m.model_id, "coverage")
    covered = g.random(S) < m.coverage_rate
    estu = covered & (g.random(S) < m.estu_rate)

    g = stream(cfg.global_seed, m.model_id, "industry_static")
    primary0 = g.integers(0, n_ind, S).astype(np.int16)
    dual = g.random(S) < m.dual_industry_frac
    secondary0 = ((primary0 + 1 + g.integers(0, n_ind - 1, S)) % n_ind).astype(np.int16)

    g = stream(cfg.global_seed, m.model_id, "vols")
    vols = np.concatenate([
        g.uniform(*m.vol_style_ann, m.n_styles),
        g.uniform(*m.vol_industry_ann, n_ind),
        [m.vol_market_ann],
    ])

    g = stream(cfg.global_seed, m.model_id, "srisk_static")
    mu_lnsig = g.normal(math.log(m.srisk_median_ann), m.srisk_mu_sd, S)

    return ModelStatic(
        covered=covered, estu=estu, dual=dual,
        primary0=primary0, secondary0=secondary0,
        vols=vols, mu_lnsig=mu_lnsig,
        triu=np.triu_indices(m.n_factors),
    )


@dataclass
class ModelState:
    X: np.ndarray          # (S, n_styles) raw AR(1) style loadings
    A: np.ndarray          # (F, k) latent covariance structure
    ln_sig: np.ndarray     # (S,) log annualized specific vol
    primary: np.ndarray    # (S,) int16 current industry
    secondary: np.ndarray  # (S,) int16
    t_next: int            # next date index this state expects to process


def fresh_state(cfg: GeneratorConfig, m: ModelConfig, static: ModelStatic) -> ModelState:
    S = cfg.n_superset
    return ModelState(
        X=np.zeros((S, m.n_styles)),
        A=np.zeros((m.n_factors, m.cov_k)),
        ln_sig=np.zeros(S),
        primary=static.primary0.copy(),
        secondary=static.secondary0.copy(),
        t_next=0,
    )


def checkpoint_path(cfg: GeneratorConfig, m: ModelConfig, year: int) -> Path:
    return Path(cfg.checkpoint_dir) / m.model_id / f"{year}.npz"


def save_checkpoint(cfg: GeneratorConfig, m: ModelConfig, year: int, state: ModelState) -> None:
    path = checkpoint_path(cfg, m, year)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(path, X=state.X, A=state.A, ln_sig=state.ln_sig,
             primary=state.primary, secondary=state.secondary,
             t_next=np.int64(state.t_next))


def load_checkpoint(cfg: GeneratorConfig, m: ModelConfig, year: int) -> ModelState:
    path = checkpoint_path(cfg, m, year)
    if not path.exists():
        raise FileNotFoundError(
            f"checkpoint {path} not found — run the preceding years first")
    z = np.load(path)
    return ModelState(X=z["X"], A=z["A"], ln_sig=z["ln_sig"],
                      primary=z["primary"], secondary=z["secondary"],
                      t_next=int(z["t_next"]))


def advance(state: ModelState, cfg: GeneratorConfig, m: ModelConfig,
            static: ModelStatic, t: int) -> tuple[np.ndarray, np.ndarray]:
    """Advance all AR processes to date index t.

    Returns (Sigma_ann, sig_ann): the annualized-decimal factor covariance
    (F, F) and annualized-decimal specific vols (S,). Date 0 initializes each
    process at its stationary distribution via eps / sqrt(1 - phi^2).
    """
    if t != state.t_next:
        raise RuntimeError(f"state expects date index {state.t_next}, got {t}")
    S = cfg.n_superset
    seed, mid = cfg.global_seed, m.model_id

    # Styles: x_t = phi x_{t-1} + eps, eps ~ N(0, 1 - phi^2)
    phi = m.style_ar_phi
    g = stream(seed, mid, "style", t)
    eps = g.normal(0.0, math.sqrt(1.0 - phi * phi), (S, m.n_styles))
    state.X = eps / math.sqrt(1.0 - phi * phi) if t == 0 else phi * state.X + eps

    # Industry reassignment
    if t > 0:
        g = stream(seed, mid, "industry_switch", t)
        switch = g.random(S) < m.industry_switch_annual / BUSDAYS_PER_YEAR
        n_sw = int(switch.sum())
        if n_sw:
            n_ind = m.n_industries
            new_p = g.integers(0, n_ind, n_sw).astype(np.int16)
            new_s = ((new_p + 1 + g.integers(0, n_ind - 1, n_sw)) % n_ind).astype(np.int16)
            state.primary[switch] = new_p
            state.secondary[switch] = new_s

    # Covariance structure: entries AR(1), stationary var 1/k
    phi_c = m.cov_ar_phi
    g = stream(seed, mid, "cov", t)
    eta = g.normal(0.0, math.sqrt((1.0 - phi_c * phi_c) / m.cov_k), (m.n_factors, m.cov_k))
    state.A = eta / math.sqrt(1.0 - phi_c * phi_c) if t == 0 else phi_c * state.A + eta
    C = state.A @ state.A.T + COV_DIAG_JITTER * np.eye(m.n_factors)
    d = np.sqrt(np.diag(C))
    R = C / np.outer(d, d)
    sigma_ann = np.outer(static.vols, static.vols) * R

    # Specific risk: AR(1) in log space around per-asset mean
    phi_s = m.srisk_ar_phi
    g = stream(seed, mid, "srisk", t)
    xi = g.normal(0.0, math.sqrt(1.0 - phi_s * phi_s) * m.srisk_logvol_sd, S)
    if t == 0:
        state.ln_sig = static.mu_lnsig + xi / math.sqrt(1.0 - phi_s * phi_s)
    else:
        state.ln_sig = static.mu_lnsig + phi_s * (state.ln_sig - static.mu_lnsig) + xi
    sig_ann = np.clip(np.exp(state.ln_sig), *m.srisk_clip_ann)

    state.t_next = t + 1
    return sigma_ann, sig_ann


def emit_loadings(state: ModelState, static: ModelStatic, m: ModelConfig,
                  live: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Nonzero loading rows for one date, sorted (asset, factor_seq).

    Returns (slot_idx int32, factor_seq int16, value float64). Style loadings
    are cross-sectionally standardized over the live estimation universe
    (population std) before emission; internal AR state stays raw.
    """
    idx = np.flatnonzero(live & static.covered).astype(np.int32)
    est = live & static.estu
    Xe = state.X[est]
    Z = (state.X[idx] - Xe.mean(axis=0)) / Xe.std(axis=0)

    ns, n_c = m.n_styles, idx.size
    dual_c = static.dual[idx]
    dual_idx = idx[dual_c]

    a = np.concatenate([
        np.repeat(idx, ns),                                   # styles (dense)
        idx,                                                  # primary industry
        dual_idx,                                             # secondary industry
        idx,                                                  # market
    ])
    f = np.concatenate([
        np.tile(np.arange(ns, dtype=np.int16), n_c),
        (ns + state.primary[idx]).astype(np.int16),
        (ns + state.secondary[dual_idx]).astype(np.int16),
        np.full(n_c, m.n_factors - 1, dtype=np.int16),
    ])
    v = np.concatenate([
        round_sig(Z.ravel()),
        np.where(dual_c, DUAL_PRIMARY_WEIGHT, 1.0),
        np.full(dual_idx.size, DUAL_SECONDARY_WEIGHT),
        np.ones(n_c),
    ])
    order = np.lexsort((f, a))
    return a[order], f[order], v[order]


def emit_covariance(sigma_ann: np.ndarray, static: ModelStatic, m: ModelConfig) -> np.ndarray:
    """Upper-triangle values (factor_seq order) in the model's cov_scaling units."""
    vals = sigma_ann[static.triu]
    if m.cov_scaling == "ann_var_pct2":
        vals = vals * 1e4
    elif m.cov_scaling == "daily_var":
        vals = vals / 252.0
    else:
        raise ValueError(f"unknown cov_scaling {m.cov_scaling!r}")
    return round_sig(vals)


def emit_specific_risk(sig_ann: np.ndarray, static: ModelStatic, m: ModelConfig,
                       live: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(slot_idx, value) per covered live asset, in the model's convention units."""
    idx = np.flatnonzero(live & static.covered).astype(np.int32)
    s = sig_ann[idx]
    if m.specific_risk_convention == "ann_vol_pct":
        s = s * 100.0
    elif m.specific_risk_convention == "daily_vol":
        s = s / math.sqrt(252.0)
    else:
        raise ValueError(f"unknown specific_risk_convention {m.specific_risk_convention!r}")
    return idx, round_sig(s)


def emit_membership(static: ModelStatic, live: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(slot_idx, estu_flag) per covered live asset."""
    idx = np.flatnonzero(live & static.covered).astype(np.int32)
    return idx, static.estu[idx]
