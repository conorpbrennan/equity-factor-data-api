# Claude Code prompts — with captured responses

Every prompt from `claude_cli_prompts.md`, run in order through a single
headless `claude -p` session (chained with `--resume`, so context-dependent
prompts like "Same book..." see the earlier turns) from the repo root with
the `factor-data` skill discovered. No real store mounted — each answer runs
against the self-building demo micro store. Captured 2026-07-14 with
Claude Code 2.1.209.


## Discovery


### Prompt 1

> What factor models are in the store, and what units does each vendor publish in?

**Response:**

No `FACTOR_STORE_ROOT` is set in this environment, so this is the **demo micro store** — it holds two models:

| model_id | vendor | variant / region | factors | factor returns | specific risk | covariance |
|---|---|---|---|---|---|---|
| `AX_TEST1_MH` | SimCorp Axioma | MH, US | 6 | daily **percent** | annualized **percent** vol | annualized **percent²** variance |
| `BARRA_TEST1_L` | MSCI Barra | L, US | 2 | daily **decimal** | daily **decimal** vol | daily **decimal** variance |

So the two vendors publish in quite different raw units: Axioma works in percent and annualized risk terms, while Barra publishes everything daily in decimals.

You only see those raw units on the strict layer (`fac.core`). The `ModelFacade` user layer normalizes both models to the same canonical units regardless of vendor: factor returns as daily decimal, specific risk as annualized decimal vol, and covariance as annualized decimal variance.

If you meant a real mounted store rather than the demo one, point `FACTOR_STORE_ROOT` at it (or tell me the path) and I'll rerun the listing.


### Prompt 2

> Describe the AX_TEST1_MH model — styles, date range, raw units.

**Response:**

**AX_TEST1_MH** (SimCorp Axioma TEST1, MH variant, US region):

- **Styles** — 3 style factors: `MARKET_SENSITIVITY`, `MT_MOMENTUM`, `VALUE`. The model has 6 factors in total, so the remaining 3 are non-style factors (market/industry).
- **Date range** — 2025-01-02 through 2025-01-15.
- **Raw vendor units** — factor returns in daily percent (`daily_pct`), specific risk as annualized percent vol (`ann_vol_pct`), and covariance as annualized percent-squared variance (`ann_var_pct2`).

As before, those raw units live on `fac.core`; the facade layer converts everything to canonical units (daily decimal returns, annualized decimal vol, annualized decimal² covariance).


## Data pulls


### Prompt 3

> Show me the latest factor loadings for AX_TEST1_MH, wide format.

**Response:**

Latest loadings for **AX_TEST1_MH** (latest cob date = 2025-01-15, all 6 assets, one column per factor):

| asset_id | MARKET_SENSITIVITY | MT_MOMENTUM | VALUE | IND01 | IND02 | MARKET |
|---|---|---|---|---|---|---|
| 1 | 0.109 | 0.119 | 0.129 | 1.0 | 0.0 | 1.0 |
| 2 | 0.209 | 0.219 | 0.229 | 0.0 | 1.0 | 1.0 |
| 3 | 0.309 | 0.319 | 0.329 | 1.0 | 0.0 | 1.0 |
| 4 | 0.409 | 0.419 | 0.429 | 0.0 | 1.0 | 1.0 |
| 5 | 0.509 | 0.519 | 0.529 | 1.0 | 0.0 | 1.0 |
| 6 | 0.609 | 0.619 | 0.629 | 0.0 | 1.0 | 1.0 |

This confirms the factor structure from `describe()`: 3 style factors, two industry dummies (odd-numbered assets in IND01, even in IND02), and a MARKET intercept of 1.0 on every asset.


### Prompt 4

> Get specific risk for AX0000001 and AX0000002 as of the latest date.

**Response:**

Specific risk as of the latest date (2025-01-15):

| vendor id | asset_id | specific risk |
|---|---|---|
| AX0000001 | 1 | 0.29 |
| AX0000002 | 2 | 0.30 |

These are canonical units — annualized decimal vol — so 29% and 30% annualized specific volatility. (The raw Axioma values are stored as annualized percent; the facade converts.)


## Analytics


### Prompt 5

> I hold 10mm of asset 1, 20mm of asset 2, and I'm short 5mm of asset 3. What are my factor exposures today?

**Response:**

Your factor exposures as of the latest date (2025-01-15), in $mm per unit loading:

| factor | exposure ($mm) |
|---|---|
| MARKET_SENSITIVITY | 3.725 |
| MT_MOMENTUM | 3.975 |
| VALUE | 4.225 |
| IND01 | 5.0 |
| IND02 | 20.0 |
| MARKET | 25.0 |

A few sanity checks that all line up: MARKET is your net position (10 + 20 − 5 = 25mm, since every asset loads 1.0), IND01 is assets 1 and 3 netted (10 − 5 = 5mm), and IND02 is asset 2 alone (20mm). The style exposures are the position-weighted loadings, e.g. VALUE = 10×0.129 + 20×0.229 − 5×0.329 = 4.225.


### Prompt 6

> Same book against a benchmark of 15mm each in assets 1 and 2 — what are my active exposures?

**Response:**

Active exposures (book minus benchmark) as of 2025-01-15, in $mm:

| factor | active exposure ($mm) |
|---|---|
| MARKET_SENSITIVITY | −1.045 |
| MT_MOMENTUM | −1.095 |
| VALUE | −1.145 |
| IND01 | −10.0 |
| IND02 | +5.0 |
| MARKET | −5.0 |

The active book nets to −5mm in asset 1, +5mm in asset 2, and −5mm in asset 3. That leaves you 5mm net short overall (MARKET), underweight industry 1 by 10mm (short legs in assets 1 and 3 both sit in IND01) and overweight industry 2 by 5mm. The style tilts are all mildly negative — mostly because asset 3, which you're short, has higher style loadings than the long overweight in asset 2 offsets.


## The show-stoppers


### Prompt 7

> What's my flash PnL for today, and how will it compare to the official number tomorrow?

**Response:**

Your flash factor PnL for 2025-01-15 totals **+$0.372mm (~$372k)**, broken down as:

| factor | exposure ($mm) | T0 est. return | flash PnL ($mm) |
|---|---|---|---|
| MARKET | 25.0 | 0.6% | 0.150 |
| IND02 | 20.0 | 0.6% | 0.120 |
| IND01 | 5.0 | 0.6% | 0.030 |
| VALUE | 4.225 | 0.6% | 0.025 |
| MT_MOMENTUM | 3.975 | 0.6% | 0.024 |
| MARKET_SENSITIVITY | 3.725 | 0.6% | 0.022 |

On the comparison with the official number: in this demo store the vendor's official returns for 2025-01-15 are already published, so I could run both side by side. The official decomposition uses identical exposures — the only thing that changes is the return stream. Officially each factor returned 0.5% versus the 0.6% T0 estimate, so the official factor PnL comes to **+$0.310mm**, meaning the flash overstates by about $62k (~20%).

That's the general pattern you'd see tomorrow in production too: exposures are locked at today's cob, and when the vendor publishes official factor returns the `T0_ESTIMATE` stream is replaced by the `OFFICIAL` one. The flash-to-official revision is purely the gap between the T0 estimates (FMP weights × observed asset returns) and the vendor's fitted factor returns.


### Prompt 8

> My momentum exposure moved over the stored history — explain where the change is coming from.

**Response:**

Your **MT_MOMENTUM exposure rose from 3.75 to 3.975 $mm (+0.225)** over the stored history (2025-01-02 → 2025-01-15). Since holdings are held constant in this analysis, the entire move is loading drift, not trading. The by-asset attribution:

| asset | position ($mm) | contribution start → end | change |
|---|---|---|---|
| 2 | +20 | 4.20 → 4.38 | **+0.18** |
| 1 | +10 | 1.10 → 1.19 | +0.09 |
| 3 | −5 | −1.55 → −1.595 | −0.045 |

What's happening: every asset's momentum loading crept up by the same 0.009 over the window (e.g. asset 2 went 0.210 → 0.219), so each position's contribution moved in proportion to its size and sign. **Asset 2 dominates** — it's your largest position at 20mm, so it accounts for +0.18 of the +0.225, with asset 1 adding +0.09 and your 5mm short in asset 3 clawing back −0.045 as its rising loading works against you.

Worth noting the same +0.225 drift shows up in MARKET_SENSITIVITY and VALUE too — the style loadings in this demo store all trend uniformly — while the industry and market exposures are flat because those are 0/1 dummies that don't drift.


## Cache & workflow


### Prompt 9

> Warm the cache for assets 1–3, run a few covered queries, and show me the hit/miss stats. Then persist the working set and prove a fresh session can start hot from it.

**Response:**

Done — the full warm → persist → hot-restart cycle works:

**Session 1 (warm + queries).** `fac.warm([1, 2, 3])` loaded the working set into memory: 150 loading rows, 30 specific-risk rows, and 60 factor-return rows (YTD loadings and specific risk for assets 1–3, plus all official factor returns). I then ran 4 covered queries — latest loadings, latest specific risk, the full factor-return history, and loadings for a mid-window date — and the stats came back **hits: 4, misses: 0**: every one was served from memory without touching the store.

**Persist.** `save_cache()` wrote the set to `/tmp/factor-demo-cache/usercache/2025-01-15/AX_TEST1_MH` — keyed by (as-of date, model), one parquet per dataset plus a coverage manifest. I pointed `FACTOR_CACHE_DIR` at a temp location for the demo; unset, it defaults to the system temp dir.

**Session 2 (fresh start).** A brand-new `ModelFacade.load(...)` followed by `load_cache()` adopted the saved set — same 150/30/60 rows, counters reset to zero for the new session. Three covered queries then scored **hits: 3, misses: 0**, proving the fresh session starts fully hot from disk with no store access.

In production this is the `warm_cache.py` morning-job pattern: a cron warms and persists per model before the risk team arrives, and every later session just calls `load_cache()`. One caveat baked into the design: the cache is coverage-based, so anything outside the warmed scope (other assets, dates beyond the as-of) transparently falls through to the store — correctness never depends on the cache.


## Guardrails (the fail-loud story)


### Prompt 10

> Ask the BARRA_TEST1_L model for estimated factor returns.

**Response:**

**BARRA_TEST1_L has no estimate stream** — the request correctly raises: `factor_return has no 'type' column — this store carries no estimate stream`.

The Barra model's factor-return data in this store predates the T0 stream: its parquet schema has only `cob_date`, `factor_id`, `value`, `version_id`, with no `type` column to distinguish `OFFICIAL` from `T0_ESTIMATE` rows. The facade refuses rather than silently handing back official numbers, so there are no estimated factor returns I can give you for this model. (In this demo store only `AX_TEST1_MH` carries the estimate stream.)

Two side notes from poking at it: the Barra model in the micro store carries *only* factor returns — no loadings, specific risk, or covariance — so the no-argument `get_factor_returns()` call also fails on date-range resolution; you have to pass explicit dates. If you want its official returns instead, `fac.get_factor_returns("2025-01-02", "2025-01-15")` works fine.


### Prompt 11

> Pass the string "2025-01-15" directly to the core Model's factor_loadings. What happens and why?

**Response:**

It raises immediately:

```
TypeError: as_of must be datetime.date, got 'str' — the core layer does not coerce; use ModelFacade for string dates
```

This is the strict/lenient split in the API design. The core `Model` layer is deliberately rigid: it accepts only `datetime.date`, returns raw vendor values in vendor units, and does no coercion or guessing — it's the layer other code builds on, so it fails fast on any ambiguity rather than silently interpreting input. The `ModelFacade` user layer is where the leniency lives: it accepts `date`, `'YYYY-MM-DD'` strings, or `'latest'` (plus vendor asset ids and unit canonicalization), converts them, and then calls the core with proper types.

So the same request works one level up: `fac.get_factor_loadings("2025-01-15")` parses the string and succeeds, while `fac.core.factor_loadings(...)` needs `date(2025, 1, 15)`. The error message itself points you at the right layer, which is the nicest touch — a type mistake at the strict layer tells you where the lenient behaviour you were expecting actually lives.


## Verification


### Prompt 12

> Run both selftests and summarize what they verify.

**Response:**

Both selftests pass — **12/12 for `modelfacade`, 7/7 for `analytics`**.

**`modelfacade` (12 checks)** verifies the data layer, roughly bottom-up:

- **Conventions and store**: naming/unit conventions are honoured (snake_case, unit scales, discouraged names) and `list_models` sees every model in the store.
- **Core strictness**: the strict layer rejects string/datetime dates and unknown models/factors, and serves sparse long-format loadings in raw vendor conventions with correct date ranges — the behaviour we saw first-hand earlier.
- **Facade leniency and canonicalization**: string dates and `'latest'` work, wide pivot fills missing cells with 0.0, vendor asset ids resolve through `asset_xref` (both explicit scheme and auto-detected), and specific risk / returns / covariance come out in canonical units.
- **T0 and infrastructure**: the estimate stream is selected via the `type` column; the pre-warm cache serves covered subsets and persists/reloads across sessions (the round-trip we demonstrated); `output="pandas"` converts only at the return boundary; and wrap/unwrap plus `describe()` round-trip cleanly between layers.

**`analytics` (7 checks)** verifies the portfolio layer on top:

- **Portfolio**: canonical construction with weights and strict `as_of` dates, plus arithmetic — building an active book with holding alignment and a guard against mismatched dates.
- **Analytics functions**: exposures are value-weighted loadings with exact expected cells; PnL decomposition works per factor on the official stream in $mm; flash PnL comes via `estimates=True` through stateless functions; `exposure_change` reports per-factor moves and by-asset attribution.
- **T0 estimation**: recomputing FMP weights × asset returns reproduces the stored estimate stream exactly — i.e. the flash numbers we used earlier are internally consistent with their inputs.

Together they cover essentially everything this session exercised: units, date leniency, id resolution, caching, estimates, and the exposure/PnL/drill-down analytics.
