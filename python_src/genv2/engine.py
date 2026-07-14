"""Per-model state, dynamics, and per-date emissions for v2 (spec §1–§2, §5).

Variant seed-aliasing: components shared with the base model (coverage,
industries, srisk, the base block of style innovations, the base block of FMP
weights) draw from streams keyed by the BASE model_id — shared factor loadings
are byte-identical to the base by construction. Variant-only components
(extra styles, covariance structure, factor returns) use the variant's id.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from generator.rng import round_sig, stream
from generator.trading_calendar import BUSDAYS_PER_YEAR

from .fleet import V2Config, V2Model
from .universe import MultiUniverse

DUAL_W = (0.65, 0.35)
COV_JITTER = 0.1


@dataclass(frozen=True)
class ModelStatic:
    domain: np.ndarray        # (S,) bool — slots in the model's regions
    covered: np.ndarray       # (S,) bool
    estu: np.ndarray          # (S,) bool
    dual: np.ndarray
    primary0: np.ndarray
    secondary0: np.ndarray
    vols: np.ndarray          # (F,) annualized vol targets, factor_seq order
    mu_lnsig: np.ndarray
    cty_seq: np.ndarray | None   # (S,) int16 factor seq of asset's country factor
    ccy_seq: np.ndarray | None
    triu: tuple


def build_static(cfg: V2Config, m: V2Model, uni: MultiUniverse) -> ModelStatic:
    S, seed = uni.n_slots, cfg.global_seed
    sid = m.seed_id(shared=True)

    domain = uni.region_mask(m.universe_regions)
    g = stream(seed, sid, "coverage")
    covered = domain & (g.random(S) < m.coverage_rate)
    estu = covered & (g.random(S) < m.estu_rate)

    g = stream(seed, sid, "industry_static")
    primary0 = g.integers(0, m.n_industries, S).astype(np.int16)
    dual = g.random(S) < m.dual_industry_frac
    secondary0 = ((primary0 + 1 + g.integers(0, m.n_industries - 1, S))
                  % m.n_industries).astype(np.int16)

    g = stream(seed, m.model_id, "vols")
    vols = np.concatenate([
        g.uniform(*m.vol_style_ann, m.n_styles),
        g.uniform(*m.vol_industry_ann, m.n_industries),
        g.uniform(*m.vol_country_ann, m.n_countries),
        g.uniform(*m.vol_ccy_ann, m.n_currencies),
        [m.vol_market_ann],
    ])

    g = stream(seed, sid, "srisk_static")
    mu_lnsig = g.normal(math.log(m.srisk_median_ann), m.srisk_mu_sd, S)

    base = m.n_styles + m.n_industries
    cty_seq = (base + (uni.country_code % m.n_countries)).astype(np.int16) \
        if m.n_countries else None
    ccy_seq = (base + m.n_countries + (uni.ccy_code % m.n_currencies)).astype(np.int16) \
        if m.n_currencies else None

    return ModelStatic(domain=domain, covered=covered, estu=estu, dual=dual,
                       primary0=primary0, secondary0=secondary0, vols=vols,
                       mu_lnsig=mu_lnsig, cty_seq=cty_seq, ccy_seq=ccy_seq,
                       triu=np.triu_indices(m.n_factors))


@dataclass
class ModelState:
    X: np.ndarray        # (S, n_styles)
    A: np.ndarray        # (F, k)
    ln_sig: np.ndarray   # (S,)
    W: np.ndarray        # (S, n_styles + 1) FMP weights state
    z: np.ndarray        # (F,) factor-return AR state
    primary: np.ndarray
    secondary: np.ndarray
    t_next: int


def fresh_state(m: V2Model, static: ModelStatic, S: int) -> ModelState:
    return ModelState(X=np.zeros((S, m.n_styles)), A=np.zeros((m.n_factors, m.cov_k)),
                      ln_sig=np.zeros(S), W=np.zeros((S, m.n_styles + 1)),
                      z=np.zeros(m.n_factors),
                      primary=static.primary0.copy(), secondary=static.secondary0.copy(),
                      t_next=0)


def checkpoint_path(cfg: V2Config, m: V2Model, year: int) -> Path:
    return Path(cfg.checkpoint_dir) / m.model_id / f"{year}.npz"


def save_checkpoint(cfg, m, year, st: ModelState) -> None:
    p = checkpoint_path(cfg, m, year)
    p.parent.mkdir(parents=True, exist_ok=True)
    np.savez(p, X=st.X, A=st.A, ln_sig=st.ln_sig, W=st.W, z=st.z,
             primary=st.primary, secondary=st.secondary, t_next=np.int64(st.t_next))


def load_checkpoint(cfg, m, year) -> ModelState:
    z = np.load(checkpoint_path(cfg, m, year))
    return ModelState(X=z["X"], A=z["A"], ln_sig=z["ln_sig"], W=z["W"], z=z["z"],
                      primary=z["primary"], secondary=z["secondary"],
                      t_next=int(z["t_next"]))


def _ar_step(prev, eps, phi, t):
    return eps / math.sqrt(1.0 - phi * phi) if t == 0 else phi * prev + eps


def advance(st: ModelState, cfg: V2Config, m: V2Model, static: ModelStatic,
            t: int, S: int) -> tuple[np.ndarray, np.ndarray]:
    """Advance all processes to date t; returns (Sigma_ann, sig_ann)."""
    if t != st.t_next:
        raise RuntimeError(f"{m.model_id}: state expects t={st.t_next}, got {t}")
    seed = cfg.global_seed
    sid_shared, sid_own = m.seed_id(True), m.model_id

    # Styles: base block from base streams (variant identity), extras own.
    phi = m.style_ar_phi
    s_eps = math.sqrt(1.0 - phi * phi)
    nb = m.n_base_styles or m.n_styles
    eps = stream(seed, sid_shared, "style", t).normal(0.0, s_eps, (S, nb))
    if m.n_styles > nb:
        extra = stream(seed, sid_own, "style_extra", t).normal(0.0, s_eps, (S, m.n_styles - nb))
        eps = np.concatenate([eps, extra], axis=1)
    st.X = _ar_step(st.X, eps, phi, t)

    if t > 0:
        g = stream(seed, sid_shared, "industry_switch", t)
        sw = g.random(S) < m.industry_switch_annual / BUSDAYS_PER_YEAR
        n_sw = int(sw.sum())
        if n_sw:
            nI = m.n_industries
            new_p = g.integers(0, nI, n_sw).astype(np.int16)
            st.primary[sw] = new_p
            st.secondary[sw] = ((new_p + 1 + g.integers(0, nI - 1, n_sw)) % nI).astype(np.int16)

    # Covariance structure (variant-specific: factor count differs from base)
    phi_c = m.cov_ar_phi
    eta = stream(seed, sid_own, "cov", t).normal(
        0.0, math.sqrt((1.0 - phi_c * phi_c) / m.cov_k), (m.n_factors, m.cov_k))
    st.A = _ar_step(st.A, eta, phi_c, t)
    C = st.A @ st.A.T + COV_JITTER * np.eye(m.n_factors)
    d = np.sqrt(np.diag(C))
    sigma_ann = np.outer(static.vols, static.vols) * (C / np.outer(d, d))

    # Specific risk (shared)
    phi_s = m.srisk_ar_phi
    xi = stream(seed, sid_shared, "srisk", t).normal(
        0.0, math.sqrt(1.0 - phi_s * phi_s) * m.srisk_logvol_sd, S)
    st.ln_sig = static.mu_lnsig + _ar_step(st.ln_sig - static.mu_lnsig, xi, phi_s, t)
    sig_ann = np.clip(np.exp(st.ln_sig), *m.srisk_clip_ann)

    # FMP weights: base block (base styles + market) shared, extra styles own.
    phi_w = m.fmp_ar_phi
    s_w = math.sqrt(1.0 - phi_w * phi_w)
    wb = stream(seed, sid_shared, "fmp", t).normal(0.0, s_w, (S, nb + 1))
    if m.n_styles > nb:
        we = stream(seed, sid_own, "fmp_extra", t).normal(0.0, s_w, (S, m.n_styles - nb))
        w_eps = np.concatenate([wb[:, :nb], we, wb[:, -1:]], axis=1)
    else:
        w_eps = wb
    st.W = _ar_step(st.W, w_eps, phi_w, t)

    # Factor returns AR state (variant-specific)
    phi_r = m.ret_ar_phi
    zeta = stream(seed, sid_own, "fret", t).normal(0.0, math.sqrt(1.0 - phi_r * phi_r),
                                                   m.n_factors)
    st.z = _ar_step(st.z, zeta, phi_r, t)

    st.t_next = t + 1
    return sigma_ann, sig_ann


# ------------------------------------------------------------------ emissions

def emit_loadings(st: ModelState, static: ModelStatic, m: V2Model,
                  live: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(slot_idx, factor_seq, value) sorted (asset, seq); styles standardized
    over live ESTU; industry 1.0/0.65+0.35; country/currency/market 1.0."""
    idx = np.flatnonzero(live & static.covered).astype(np.int32)
    est = live & static.estu
    Xe = st.X[est]
    Z = (st.X[idx] - Xe.mean(axis=0)) / Xe.std(axis=0)

    ns, nC = m.n_styles, idx.size
    dual_c = static.dual[idx]
    dual_idx = idx[dual_c]
    parts_a = [np.repeat(idx, ns), idx, dual_idx]
    parts_f = [np.tile(np.arange(ns, dtype=np.int16), nC),
               (ns + st.primary[idx]).astype(np.int16),
               (ns + st.secondary[dual_idx]).astype(np.int16)]
    parts_v = [round_sig(Z.ravel()),
               np.where(dual_c, DUAL_W[0], 1.0),
               np.full(dual_idx.size, DUAL_W[1])]
    if static.cty_seq is not None:
        parts_a.append(idx); parts_f.append(static.cty_seq[idx]); parts_v.append(np.ones(nC))
    if static.ccy_seq is not None:
        parts_a.append(idx); parts_f.append(static.ccy_seq[idx]); parts_v.append(np.ones(nC))
    parts_a.append(idx)
    parts_f.append(np.full(nC, m.n_factors - 1, dtype=np.int16))
    parts_v.append(np.ones(nC))

    a = np.concatenate(parts_a); f = np.concatenate(parts_f); v = np.concatenate(parts_v)
    order = np.lexsort((f, a))
    return a[order], f[order], v[order]


def emit_covariance(sigma_ann, static: ModelStatic, m: V2Model) -> np.ndarray:
    vals = sigma_ann[static.triu]
    vals = vals * 1e4 if m.cov_scaling == "ann_var_pct2" else vals / 252.0
    return round_sig(vals)


def emit_specific_risk(sig_ann, static: ModelStatic, m: V2Model, live):
    idx = np.flatnonzero(live & static.covered).astype(np.int32)
    s = sig_ann[idx]
    s = s * 100.0 if m.specific_risk_convention == "ann_vol_pct" else s / math.sqrt(252.0)
    return idx, round_sig(s)


def emit_membership(static: ModelStatic, live):
    idx = np.flatnonzero(live & static.covered).astype(np.int32)
    return idx, static.estu[idx]


def emit_factor_returns(st: ModelState, static: ModelStatic, m: V2Model) -> np.ndarray:
    r = static.vols / math.sqrt(252.0) * st.z
    if m.return_convention == "daily_pct":
        r = r * 100.0
    return round_sig(r)   # length n_factors, factor_seq order


def emit_fmp(st: ModelState, static: ModelStatic, m: V2Model,
             live) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(factor_seq, slot_idx, weight) for styles+market over live ESTU,
    unit gross per factor, sorted (factor_seq, asset)."""
    idx = np.flatnonzero(live & static.estu).astype(np.int32)
    W = st.W[idx]                                  # (n_estu, ns+1)
    W = W / np.abs(W).sum(axis=0)                  # unit gross per factor
    n, k = W.shape
    seqs = np.asarray(m.fmp_factor_seq, dtype=np.int16)
    f = np.repeat(seqs, n)
    a = np.tile(idx, k)
    v = round_sig(W.T.ravel())
    return f, a, v


def emit_asset_returns(st: ModelState, static: ModelStatic, m: V2Model,
                       cfg, live, sig_ann, t: int,
                       la, lf, lv) -> tuple[np.ndarray, np.ndarray]:
    """(slot_idx, value) per-asset total returns, vendor return convention.

    Model-coherent: r_a = Σ_f L_af · fr_f + ε_a · σ_a/√252, built from the
    same (rounded) loadings this date emits, the decimal factor returns,
    and idio noise scaled by the day's specific risk. The 'aret' stream is
    new and counter-keyed, so no existing series is perturbed. Variants
    share the base's idio draw (same vendor, same asset)."""
    fr_dec = static.vols / math.sqrt(BUSDAYS_PER_YEAR) * st.z
    fac_part = np.zeros(sig_ann.shape[0])
    np.add.at(fac_part, la, lv * fr_dec[lf])
    eps = stream(cfg.global_seed, m.seed_id(True), "aret", t).normal(
        0.0, 1.0, sig_ann.shape[0])
    idx = np.flatnonzero(live & static.covered).astype(np.int32)
    r = fac_part[idx] + eps[idx] * sig_ann[idx] / math.sqrt(BUSDAYS_PER_YEAR)
    if m.return_convention == "daily_pct":
        r = r * 100.0
    return idx, round_sig(r)


def emit_t0_estimates(m: V2Model, ff, fa, fw, ar_idx, ar_val,
                      n_slots: int) -> np.ndarray:
    """T0_ESTIMATE factor returns: Σ_a w_fa · r_a over the FMPs, computed
    from the rounded as-stored weights and asset returns (vendor units) so
    a consumer recomputing from the store recovers these numbers. Length
    len(fmp_factor_seq), that order."""
    r_full = np.zeros(n_slots)
    r_full[ar_idx] = ar_val
    k = len(m.fmp_factor_seq)
    n = len(fa) // k
    est = (fw.reshape(k, n) * r_full[fa[:n]][None, :]).sum(axis=1)
    return round_sig(est)
