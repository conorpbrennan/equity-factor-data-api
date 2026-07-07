# Generator Spec — Synthetic Multi-Model Equity Factor Data

Companion to `factor-model-benchmark-plan.md`. This pins every parameter and algorithm so implementation is mechanical. Anything not specified here is an implementation detail; anything specified here is a contract — the benchmark's honesty depends on the data's compression behavior, which these choices control.

---

## 1. Scope of output

The generator writes the **normalized Parquet store only** (the "as-is Snowflake" baseline). Transforms A/B are downstream DuckDB SQL, out of scope here.

Tables produced:

| Table | Kind | Written |
|---|---|---|
| `model_master` | dimension | once |
| `factor_master` | dimension | once |
| `asset_master` | dimension | once |
| `asset_xref` | dimension | once |
| `factor_loading` | fact | per model × year |
| `factor_covariance` | fact | per model × year |
| `specific_risk` | fact | per model × year |
| `universe_membership` | fact | per model × year |

---

## 2. Parameter block

Single TOML/dataclass config; two levels — global and per-model.

```python
@dataclass
class GeneratorConfig:
    global_seed: int = 20260707

    # Calendar
    start_date: date = date(2006, 1, 2)
    end_date: date   = date(2025, 12, 31)
    calendar: str    = "weekdays"          # Mon–Fri, no holiday calendar → 5,218 COB dates

    # Universe (shared across models; coverage is per-model)
    n_live: int          = 3_000           # live names on any COB date (held exactly)
    n_superset: int      = 6_500           # total asset IDs incl. reserve pool (assert never exhausted)
    annual_churn: float  = 0.05            # expected exits/yr as fraction of n_live → ~3,000 replacements over 20y

    # Output
    output_dir: str      = "data/normalized"
    compression: str     = "zstd"          # level 3
    row_group_bytes: int = 128 * 2**20     # uncompressed target; stats + dictionary encoding ON
    checkpoint_dir: str  = "data/checkpoints"

    models: list[ModelConfig] = [BARRA_USE4_L, AXIOMA_US4_MH]
```

```python
@dataclass
class ModelConfig:
    model_id: str
    vendor: str
    style_factors: list[str]               # names below
    n_industries: int
    industry_prefix: str                    # "IND" / "SEC"
    market_factor: str                      # "COUNTRY" / "MARKET"
    cov_scaling: str                        # "ann_var_pct2" | "daily_var"
    specific_risk_convention: str           # "ann_vol_pct" | "daily_vol"

    # Dynamics
    style_ar_phi: float = 0.99              # AR(1) on style loadings
    cov_ar_phi: float   = 0.997             # AR(1) on covariance factor-structure entries
    srisk_ar_phi: float = 0.995             # AR(1) on log specific vol
    industry_switch_annual: float = 0.01    # per-asset prob/yr of industry reassignment
    dual_industry_frac: float = 0.10        # fraction of assets loading on 2 industries (0.65/0.35)

    # Covariance structure
    cov_k: int = 10                         # rank of latent structure A (n_factors × k)
    vol_market_ann: float = 0.16            # target annualized factor vols
    vol_industry_ann: tuple = (0.15, 0.35)  # U(lo, hi), drawn once per factor
    vol_style_ann: tuple    = (0.02, 0.10)

    # Specific risk (annualized, internal units)
    srisk_median_ann: float = 0.28          # per-asset mean of log-vol ~ N(ln 0.28, 0.25²)
    srisk_logvol_sd: float  = 0.35          # stationary sd of ln σ around asset mean
    srisk_clip_ann: tuple   = (0.05, 1.00)

    # Coverage
    coverage_rate: float = 0.985            # P(model covers a live asset), static per (model, asset)
    estu_rate: float     = 0.90             # P(covered asset is in estimation universe), static
```

### The two pinned model configs

| | `BARRA_USE4_L` | `AXIOMA_US4_MH` |
|---|---|---|
| vendor | MSCI Barra | SimCorp Axioma |
| styles (12 / 13) | BETA, MOMENTUM, SIZE, EARNYLD, RESVOL, GROWTH, BTOP, LEVERAGE, LIQUIDTY, SIZENL, DIVYLD, SENTMT | MARKET_SENSITIVITY, MT_MOMENTUM, ST_MOMENTUM, SIZE, VALUE, GROWTH, LEVERAGE, LIQUIDITY, VOLATILITY, EXCHANGE_RATE_SENS, DIVIDEND_YIELD, PROFITABILITY, EARNINGS_YIELD |
| industries | 60 (IND01–IND60) | 68 (SEC01–SEC68) |
| market factor | COUNTRY | MARKET |
| **total factors** | **73** | **82** |
| cov scaling | `ann_var_pct2` — annualized variance of % returns (Σ_ann × 10⁴) | `daily_var` — daily variance, decimal (Σ_ann / 252) |
| specific risk | `ann_vol_pct` — e.g. `27.5` | `daily_vol` — e.g. `0.0176` |

The deliberate convention mismatch is the point (plan §2.3): the same underlying risk shows up as `15.77` in one model and `0.0152` in the other. The generator produces both from one internal annualized-decimal representation; conversion happens only at write time.

---

## 3. Seed scheme

Reproducibility contract: **same config ⇒ byte-identical Parquet**, and any single year is regenerable without replaying prior years.

- **Counter-based RNG**: `numpy.random.Generator(Philox(key, counter))`.
- **Key derivation**: `key = SHA-256(global_seed ‖ model_id ‖ component)[:16]`, where `component ∈ {universe, industry, style, cov, srisk, coverage, vols}`. Model-independent components (`universe`) use `model_id = "_"`.
- **Counter = COB date index** (0-based position in the trading calendar) for per-date draws; counter = 0 for one-time draws (industry assignment, vol targets, coverage flags).
- **AR(1) state problem**: recursive processes need history. Solution: the generator runs sequentially but **checkpoints full state at each year boundary** (`checkpoint_dir/{model_id}/{year}.npz`: style loading matrix, covariance structure `A`, log specific vols, industry assignments, live set + reserve cursor). Regenerating year Y loads checkpoint Y−1 and replays one year. Checkpoints are part of the reproducibility contract and are cheap (~5 MB each).

Restatement hook (deferred, schema-ready): a future `inject_restatements(dates, seed)` writes additional rows with `version_id = 2` as seeded perturbations of version-1 values. Nothing else in this spec changes.

---

## 4. Algorithms

### 4.1 Calendar

All weekdays in `[start_date, end_date]` — **5,218 COB dates** (~261/yr; the plan's ~5,040 assumed holidays — the delta is noise, and skipping a holiday calendar removes a dependency).

### 4.2 Universe lifecycle (shared across models)

- Assets `1..3000` live at `start_date` (`start_date` = calendar start for the initial cohort).
- Daily exit hazard per live asset: `h = annual_churn / 261`. Each date, exits drawn `Binomial(n_live, h)` (component `universe`, counter = date index); exiting assets chosen uniformly among live names. Each exit immediately activates the next reserve asset (IDs `3001..6500`, in order), so live count stays exactly 3,000.
- `asset_master.start_date/end_date` record the lifecycle; `end_date` NULL if live at calendar end. Expected consumption: ~3,000 reserve names; assert pool never exhausts.
- `ticker`: deterministic base-26 encoding of `asset_id` (`AAA`, `AAB`, …). `sector`: one of 11 GICS-like labels, seeded static draw. `country = 'US'`.
- `asset_xref`: two rows per asset — `('BARRA', 'USA' + base36(asset_id) zero-padded to 5)` and `('AXIOMA', 'AX' + zero-padded decimal to 7)`.

### 4.3 Coverage and estimation universe (per model)

- Static per (model, asset): covered iff `u₁ < coverage_rate`, in ESTU iff covered and `u₂ < estu_rate` (component `coverage`, counter 0).
- `universe_membership` gets one row per **covered live** asset per date (presence = coverage; `estimation_universe_flag` marks ESTU). ~2,955 rows/date/model.

### 4.4 Style loadings — AR(1) + cross-sectional standardization

Per model, per style factor, per asset (dense):

```
x_t = φ · x_{t-1} + ε_t,   ε_t ~ N(0, 1 − φ²),   φ = style_ar_phi = 0.99
```

- New entrants initialize `x ~ N(0, 1)` on their entry date.
- After the AR step, **re-standardize cross-sectionally per (date, factor)**: demean and rescale to unit variance over ESTU assets, apply to all covered assets. This matches vendor z-score conventions, preserves autocorrelation, and keeps values in a realistic ±4 band. No winsorization.
- Innovations: component `style`, counter = date index; shape `(n_live_slots, n_styles)` drawn once per date.

### 4.5 Industry and market loadings

- At entry each asset draws a primary industry uniformly over the model's industries (component `industry`, counter 0, keyed by asset). `dual_industry_frac` of assets also draw a secondary industry: loadings **0.65 / 0.35**; the rest load **1.0** on the primary.
- Reassignment: per-date per-asset prob `industry_switch_annual / 261`; on switch, redraw primary (secondary structure redrawn too).
- Market factor loading = **1.0** for every covered asset (Barra country-factor convention; also maximally compressible, which is realistic).

### 4.6 Sparsity — what gets a row

**`factor_loading` stores nonzero loadings only.** A (date, asset) contributes: all styles (dense) + 1–2 industry rows + 1 market row ≈ **14.1 rows (Barra) / 15.1 rows (Axioma)** — not 73/82. This matches vendor delivery files and is the plan's §1 point that dense generation overstates storage. The wide transforms re-densify (0.0 for absent industry columns).

### 4.7 Factor covariance — evolving PSD structure

Per model, internal representation is an **annualized decimal covariance** Σ_t:

1. Latent structure `A_t` (n_factors × k, k = `cov_k`), entries AR(1) with φ = `cov_ar_phi` = 0.997, initialized `N(0, 1/√k)`, innovations `N(0, (1−φ²)/k)` (component `cov`, counter = date index).
2. Raw covariance `C_t = A_t·A_tᵀ + 0.1·I` → normalize to correlation `R_t` (unit diagonal). PSD by construction, evolving slowly.
3. Per-factor target annualized vols `v` (static, component `vols`, counter 0): market = `vol_market_ann`; industries ~ `U(vol_industry_ann)`; styles ~ `U(vol_style_ann)`.
4. `Σ_t = diag(v) · R_t · diag(v)`. Factor vols static, correlations evolve — sufficient for compression realism; time-varying vols are a possible later lever, not v1.
5. Write upper triangle including diagonal, `factor_seq(f1) ≤ factor_seq(f2)` (ordinals from `factor_master`, not lexicographic): 2,701 rows/date (Barra), 3,403 (Axioma).
6. Convention at write: `ann_var_pct2` ⇒ `Σ × 10⁴`; `daily_var` ⇒ `Σ / 252`.

Validity check per date: `min eigenvalue(R_t) > 1e-10`.

### 4.8 Specific risk

Per covered asset, AR(1) in log space around a per-asset level:

```
ln σ_t = μ_a + φ · (ln σ_{t-1} − μ_a) + ξ_t,   φ = srisk_ar_phi = 0.995
μ_a ~ N(ln srisk_median_ann, 0.25²)             (static, per asset)
ξ_t ~ N(0, (1−φ²) · srisk_logvol_sd²)
```

Clip σ_t to `srisk_clip_ann`. Convention at write: `ann_vol_pct` ⇒ `σ × 100`; `daily_vol` ⇒ `σ / √252`. Component `srisk`, counter = date index.

### 4.9 Value precision

All emitted `value` columns are rounded to **7 significant digits** at write time. Vendor files ship text at ~6–7 significant digits, and full-entropy float64 mantissas would make compression unrealistically pessimistic — the same argument as AR(1) persistence, applied to the mantissa. Internal AR state stays full-precision; rounding is an emission transform only.

---

## 5. Normalized DDL

DuckDB dialect; PK/FK comments are documentary (Parquet is the store — constraints are asserted by the validation suite, not enforced by an engine).

```sql
CREATE TABLE model_master (
    model_id                  VARCHAR NOT NULL,   -- PK
    vendor                    VARCHAR NOT NULL,
    model_name                VARCHAR NOT NULL,
    variant                   VARCHAR,
    region                    VARCHAR NOT NULL,
    n_factors                 SMALLINT NOT NULL,
    cov_scaling               VARCHAR NOT NULL,   -- 'ann_var_pct2' | 'daily_var'
    specific_risk_convention  VARCHAR NOT NULL    -- 'ann_vol_pct' | 'daily_vol'
);

CREATE TABLE factor_master (
    model_id     VARCHAR  NOT NULL,
    factor_id    VARCHAR  NOT NULL,               -- mnemonic, e.g. 'MOMENTUM', 'IND07'
    factor_seq   SMALLINT NOT NULL,               -- stable ordinal: pivot column order, cov triangle order
    factor_name  VARCHAR  NOT NULL,
    factor_type  VARCHAR  NOT NULL                -- 'STYLE' | 'INDUSTRY' | 'MARKET'
    -- PK (model_id, factor_id); UNIQUE (model_id, factor_seq)
);

CREATE TABLE asset_master (
    asset_id    INTEGER NOT NULL,                 -- PK, internal firm-scoped id
    ticker      VARCHAR NOT NULL,
    sector      VARCHAR NOT NULL,
    country     VARCHAR NOT NULL,
    start_date  DATE    NOT NULL,
    end_date    DATE                              -- NULL = live at calendar end
);

CREATE TABLE asset_xref (
    asset_id         INTEGER NOT NULL,
    vendor           VARCHAR NOT NULL,            -- 'BARRA' | 'AXIOMA'
    vendor_asset_id  VARCHAR NOT NULL
    -- PK (asset_id, vendor); UNIQUE (vendor, vendor_asset_id)
);

CREATE TABLE factor_loading (
    model_id    VARCHAR  NOT NULL,
    cob_date    DATE     NOT NULL,
    asset_id    INTEGER  NOT NULL,
    factor_id   VARCHAR  NOT NULL,
    value       DOUBLE   NOT NULL,                -- nonzero rows only (§4.6)
    version_id  SMALLINT NOT NULL DEFAULT 1
    -- PK (model_id, cob_date, asset_id, factor_id, version_id)
);

CREATE TABLE factor_covariance (
    model_id     VARCHAR  NOT NULL,
    cob_date     DATE     NOT NULL,
    factor_id_1  VARCHAR  NOT NULL,               -- factor_seq(f1) <= factor_seq(f2)
    factor_id_2  VARCHAR  NOT NULL,
    value        DOUBLE   NOT NULL,               -- in model_master.cov_scaling units
    version_id   SMALLINT NOT NULL DEFAULT 1
);

CREATE TABLE specific_risk (
    model_id    VARCHAR  NOT NULL,
    cob_date    DATE     NOT NULL,
    asset_id    INTEGER  NOT NULL,
    value       DOUBLE   NOT NULL,                -- in model_master.specific_risk_convention units
    version_id  SMALLINT NOT NULL DEFAULT 1
);

CREATE TABLE universe_membership (
    model_id                   VARCHAR NOT NULL,
    cob_date                   DATE    NOT NULL,
    asset_id                   INTEGER NOT NULL,  -- presence = model covers asset on date
    estimation_universe_flag   BOOLEAN NOT NULL
);
```

`value` stays DOUBLE — that's the honest Snowflake baseline. A float32 variant is a one-flag storage experiment for later, not part of v1.

---

## 6. Physical output layout

```
data/normalized/
  model_master.parquet
  factor_master.parquet
  asset_master.parquet
  asset_xref.parquet
  factor_loading/       model_id=<M>/year=<YYYY>/data.parquet
  factor_covariance/    model_id=<M>/year=<YYYY>/data.parquet
  specific_risk/        model_id=<M>/year=<YYYY>/data.parquet
  universe_membership/  model_id=<M>/year=<YYYY>/data.parquet
```

- Hive-partitioned by `model_id`, `year` (partition columns not repeated in-file).
- In-file sort: `(cob_date, asset_id, factor_seq-order)` — date-clustered, the typical warehouse clustering being benchmarked.
- PyArrow writer: zstd level 3, dictionary encoding on, column statistics on, row groups ≈ 128 MB uncompressed (~3–4M `factor_loading` rows).

---

## 7. Expected volumes (2 models, 5,218 dates)

| Table | Rows | Basis |
|---|---|---|
| `factor_loading` | **~450M** (217M Barra + 233M Axioma) | ~2,955 covered × ~14.1/15.1 nonzero loadings × 5,218 |
| `factor_covariance` | ~31.9M | (2,701 + 3,403) × 5,218 |
| `specific_risk` | ~30.8M | 2,955 × 2 × 5,218 |
| `universe_membership` | ~30.8M | same |
| dimensions | ~20K | — |

Estimated compressed footprint: **~4–7 GB** — below the plan's 8–14 GB band because that band assumed dense loadings rows; sparse storage (§4.6) is the realistic choice and the wide transforms restore density downstream. The dense logical grid the plan quotes (2 × 1.36B values) is what the transforms materialize.

Runtime target: full 20-year, 2-model generation in **< 15 min** single-threaded NumPy on a laptop; memory bound by one date's wide arrays (~5 MB) plus one year's accumulated output (~1 GB before write).

---

## 8. Validation suite (run after generation, blocking)

1. **Determinism**: regenerate one mid-range year from checkpoint ⇒ byte-identical Parquet.
2. **Universe**: exactly 3,000 live per date; superset ≤ 6,500; no loading/srisk/membership row outside `[start_date, end_date]` of its asset; no row for a non-covered (model, asset).
3. **Persistence**: pooled lag-1 autocorrelation of style loadings ∈ [0.98, 0.995] (sample autocorr is biased low by ~(1+3ρ)/n, so the band must tolerate short calendars); industry assignment switch rate ≈ 1%/yr measured over all assets.
4. **Cross-section**: per (date, style) mean ≈ 0, sd ≈ 1 over ESTU.
5. **Covariance**: every date's correlation min-eigenvalue > 1e-10; triangle row count exactly n(n+1)/2; ordinal convention holds.
6. **Conventions**: for a sampled asset held in both models, Barra `ann_vol_pct` ≈ Axioma `daily_vol × √252 × 100` for the *internal* value before per-model noise (they use independent draws — check units by magnitude band instead: Barra srisk ∈ [5, 100], Axioma ∈ [0.003, 0.063]).
7. **Compression sanity**: `factor_loading` vs a dense i.i.d.-noise control (the plan-§1 strawman generator, spot-generated for one month). Two metrics: bytes per **asset-date** must be < 0.5× the control (sparsity is the dominant effect — measured ~0.17×), and bytes per **row** must be < 0.95× (the value-entropy effect of AR(1) + 7-sig-digit rounding — measured ~0.86×). Note: byte-stream-split encoding was tested and rejected — the constant industry/market loadings compress better under dictionary encoding.
8. **Referential integrity**: every `factor_id` in facts exists in `factor_master`; every `asset_id` in `asset_master`; every fact `model_id` in `model_master`.

---

## 9. CLI shape

```
python -m generator generate --config config.toml                 # full run
python -m generator generate --config config.toml --years 2013    # one year from checkpoint 2012
python -m generator validate --config config.toml                 # suite §8
```

---

## Deferred (schema-ready, explicitly not v1)

- Restatement injection (`version_id ≥ 2`).
- Time-varying factor vols.
- Third model config (adding one must require zero schema code — that's the §2 design test).
- float32 `value` storage variant.
