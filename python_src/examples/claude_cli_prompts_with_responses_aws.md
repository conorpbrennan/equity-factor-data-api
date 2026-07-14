# Claude Code prompts against the AWS store — with captured responses

The `claude_cli_prompts.md` scenarios adapted to the real S3 store
(`FACTOR_STORE_ROOT=s3://equity-factor-data-651406457779/v2`): micro-store
model ids swapped for their AWS equivalents (`AX_WW4_MH`, `BARRA_USE4_L`),
dates for the store's 2006-01-02 → 2025-12-31 range. Run in order through a
single headless `claude -p` session chained with `--resume`, authenticating
S3 reads with the dedicated `AWS_FACTOR_READER_*` keys. Captured 2026-07-14
with Claude Code 2.1.209.

Queries execute on the in-region EC2 DuckDB query engine: `Store.sql()`
POSTs SQL to the box the jump service launches on demand (Arrow IPC back),
so scans run next to the data and repeat touches hit the engine's warm
range cache. The box self-terminates after 15 minutes idle.

One deliberate behaviour change from the micro-store transcript: every model
in the AWS store carries both OFFICIAL and T0_ESTIMATE return streams, so
the Barra estimated-returns prompt now returns real estimates instead of the
no-estimate-stream refusal.


## Timings: local S3 scans vs the in-region engine


End-to-end wall clock per prompt (one full Claude turn: snippet writing,
query execution, and answer synthesis — so model time is a large fixed
share; the engine effect shows in the data-heavy prompts). The local-scan
column is the same prompt suite run before `Store.sql()` was routed through
the engine, scanning S3 from the laptop over `httpfs`.


| # | prompt | local S3 scans | via engine |
|---|---|---:|---:|
| 1 | models + vendor units | 47s | 35s |
| 2 | describe AX_WW4_MH | 50s | 46s |
| 3 | latest loadings, wide | 77s | 58s |
| 4 | specific risk via vendor ids | 46s | 29s |
| 5 | book exposures | 44s | 47s |
| 6 | active vs benchmark | 68s | 38s |
| 7 | flash vs official PnL | 59s | 60s |
| 8 | momentum change drill-down | 87s | 36s |
| 9 | cache warm/persist/hot-start | 225s | 99s |
| 10 | Barra estimated returns | 307s † | 39s |
| 11 | string date into core | 52s | 35s |
| 12 | both selftests | 36s | 26s |
| | **total** | **1099s** | **546s** |

† inflated by an infrastructure event during the first run (the query
box hit its 4-hour self-terminate mid-prompt and relaunched); the other
rows are like-for-like.


## Discovery


### Prompt 1 — 35s

> What factor models are in the store, and what units does each vendor publish in?

**Response:**

The store holds 12 models from two vendors — SimCorp Axioma and MSCI Barra — and each vendor is consistent about its raw units across all of its models.

| model_id | vendor | region | factors | notes |
|---|---|---|---|---|
| AX_WW4_MH | SimCorp Axioma | Worldwide | 248 | |
| AX_US4_MH | SimCorp Axioma | US | 82 | |
| AX_EU4_MH | SimCorp Axioma | EU | 95 | |
| AX_JP4_MH | SimCorp Axioma | JP | 47 | |
| AX_EM4_MH | SimCorp Axioma | EM | 122 | |
| AX_WW4_MH_SFM1 | SimCorp Axioma | Worldwide | 251 | derived from AX_WW4_MH |
| BARRA_GEM_L | MSCI Barra | Global | 233 | |
| BARRA_USE4_L | MSCI Barra | US | 73 | |
| BARRA_EUE4_L | MSCI Barra | EU | 99 | |
| BARRA_JPE4_L | MSCI Barra | JP | 43 | |
| BARRA_EME4_L | MSCI Barra | EM | 115 | |
| BARRA_USE4_L_SFM1 | MSCI Barra | US | 75 | derived from BARRA_USE4_L |

All Axioma models are the MH (medium-horizon) variant; all Barra models are the L (long-horizon) variant. The two `_SFM1` entries are derived models built on a base model (a few extra factors on top of the parent).

**Raw vendor units** (what each vendor publishes):

- **SimCorp Axioma** — everything on a daily decimal basis: factor returns as daily decimals, covariance as daily variance, specific risk as daily volatility.
- **MSCI Barra** — mixed percent basis: factor returns as daily percent, covariance as annualized variance in percent², specific risk as annualized volatility in percent.

Note that these raw units only matter if you go through the strict `fac.core` layer. The `ModelFacade` user layer normalizes both vendors to canonical units — returns as daily decimal, specific risk as annualized decimal vol, covariance as annualized decimal variance — so cross-vendor comparisons work without unit juggling.


### Prompt 2 — 46s

> Describe the AX_WW4_MH model — styles, date range, raw units.

**Response:**

**AX_WW4_MH** is SimCorp Axioma's worldwide medium-horizon model covering US/EU/JP/EM/ROW, with 248 factors in total.

**Styles** — 16 style factors:

MARKET_SENSITIVITY, MT_MOMENTUM, ST_MOMENTUM, SIZE, VALUE, GROWTH, LEVERAGE, LIQUIDITY, VOLATILITY, EXCHANGE_RATE_SENS, DIVIDEND_YIELD, PROFITABILITY, EARNINGS_YIELD, CROWDING, ESG_MOMENTUM, SHORT_INTEREST

(The remaining factors beyond these styles are the industry/country/currency and market blocks that make up the rest of the 248.)

**Date range** — 2006-01-02 through 2025-12-31.

**Raw vendor units** — everything on a daily decimal basis:
- Factor returns: daily decimal (`daily_dec`)
- Specific risk: daily volatility (`daily_vol`)
- Covariance: daily variance (`daily_var`)

These are the raw units you'd see through the strict `fac.core` layer. The facade itself hands back canonical units — daily decimal returns, annualized decimal vol, annualized decimal² covariance — so the specific risk and covariance get annualized on the way out. It's not a derived model (`base_model_id` is null); it's the base that AX_WW4_MH_SFM1 builds on.


## Data pulls


### Prompt 3 — 58s

> Show me the latest factor loadings for AX_WW4_MH for assets 1-6, wide format.

**Response:**

Here are the latest loadings (cob 2025-12-31). One thing to flag up front: **only assets 1, 4, and 5 have loadings in AX_WW4_MH on that date — assets 2, 3, and 6 aren't covered by this model**, so the wide frame comes back with 3 rows.

The full wide frame is 3 rows × 24 columns (`cob_date`, `asset_id`, then one column per factor — the facade only returns factor columns relevant to the requested assets, not all 248). The 16 style loadings, transposed for readability:

| factor | asset 1 | asset 4 | asset 5 |
|---|---:|---:|---:|
| MARKET_SENSITIVITY | -0.2146 | 0.6960 | -0.8396 |
| MT_MOMENTUM | -0.6896 | -0.0558 | -0.9762 |
| ST_MOMENTUM | 1.0238 | 1.0903 | -0.5679 |
| SIZE | -0.3499 | 0.6858 | 0.2398 |
| VALUE | -0.3150 | 0.4541 | 0.2086 |
| GROWTH | -0.4847 | -0.6265 | 0.2598 |
| LEVERAGE | -0.2627 | 1.7361 | -0.1529 |
| LIQUIDITY | -0.1405 | -0.8246 | -0.7548 |
| VOLATILITY | 0.2796 | -0.4341 | 0.9726 |
| EXCHANGE_RATE_SENS | -0.2746 | -1.2369 | 0.4553 |
| DIVIDEND_YIELD | 1.1882 | 1.0635 | -0.9977 |
| PROFITABILITY | -0.7724 | 0.2059 | 0.0066 |
| EARNINGS_YIELD | 0.3511 | 0.3268 | 0.9445 |
| CROWDING | -0.6826 | 0.8715 | -0.1644* |
| ESG_MOMENTUM | 2.7691 | 0.8614 | -0.1965 |
| SHORT_INTEREST | 1.7699 | -0.7346 | 1.1024 |

*CROWDING for asset 5 is 0.1644 (positive) — typo guard: the frame value is 0.1644.

Beyond the styles, the remaining columns are 0/1 membership dummies: each asset sits in one sector (asset 1 → SEC17, asset 4 → SEC60, asset 5 → SEC04), and all three share CTY01, CCY01, and MARKET = 1.

A few standouts: asset 1 has an unusually high ESG_MOMENTUM loading (2.77) and elevated SHORT_INTEREST (1.77); asset 4 carries the biggest LEVERAGE tilt (1.74); asset 5 is the high-volatility, negative-momentum name of the three.


### Prompt 4 — 29s

> Get specific risk for AX0000001 and AX0000002 as of the latest date.

**Response:**

Only one of the two came back:

| vendor id | asset_id | cob_date | specific risk (ann. decimal vol) |
|---|---|---|---|
| AX0000001 | 1 | 2025-12-31 | 0.1790 (≈ 17.9% annualized) |

**AX0000002 (asset 2) has no specific-risk record in AX_WW4_MH on the latest date** — consistent with what we saw on the loadings query, where asset 2 also had no coverage in this model on 2025-12-31. So AX0000001 carries about 17.9% annualized idiosyncratic volatility (the facade's canonical unit; the raw Axioma value is daily vol, annualized on the way out), and AX0000002 simply isn't in this model's estimation universe on that date. If you need asset 2, it may be covered by one of the regional models or on an earlier date — happy to check.


## Analytics


### Prompt 5 — 47s

> I hold 10mm of asset 1, 20mm of asset 2, and I'm short 5mm of asset 3. What are my factor exposures today?

**Response:**

Here are the exposures — but with a major caveat first: **only your 10mm of asset 1 is actually in these numbers.** Assets 2 and 3 have no loadings in AX_WW4_MH on the latest date (2025-12-31), as we saw earlier, so the analytics silently exclude them. You can confirm it from the MARKET row: it shows $10mm, not the $25mm net (10 + 20 − 5) you'd get if all three positions were covered. Every figure below is just asset 1's loading × $10mm.

Exposures in $mm per unit loading, cob 2025-12-31:

| factor | exposure ($mm) | | factor | exposure ($mm) |
|---|---:|---|---|---:|
| ESG_MOMENTUM | 27.69 | | LIQUIDITY | -1.40 |
| SHORT_INTEREST | 17.70 | | MARKET_SENSITIVITY | -2.15 |
| DIVIDEND_YIELD | 11.88 | | LEVERAGE | -2.63 |
| ST_MOMENTUM | 10.24 | | EXCHANGE_RATE_SENS | -2.75 |
| SEC17 / CTY01 / CCY01 / MARKET | 10.00 each | | VALUE | -3.15 |
| EARNINGS_YIELD | 3.51 | | SIZE | -3.50 |
| VOLATILITY | 2.80 | | GROWTH | -4.85 |
| | | | CROWDING | -6.83 |
| | | | MT_MOMENTUM | -6.90 |
| | | | PROFITABILITY | -7.72 |

Taken at face value, the book is long ESG momentum, short interest, and dividend yield, and short profitability, medium-term momentum, and crowding — but that's really just asset 1's factor profile scaled by $10mm.

Given that two-thirds of your gross ($25mm of $35mm) is in uncovered names, I'd treat this as incomplete. Options: run it against a model that covers assets 2 and 3 (one of the regional models may), or use an earlier cob date where they were in the WW4 universe. Want me to check which models cover assets 2 and 3?


### Prompt 6 — 38s

> Same book against a benchmark of 15mm each in assets 1 and 2 — what are my active exposures?

**Response:**

The active book is asset 1: −5mm, asset 2: +5mm, asset 3: −5mm (your book minus the benchmark). Active exposures, cob 2025-12-31, in $mm per unit loading:

| factor | active exposure ($mm) | | factor | active exposure ($mm) |
|---|---:|---|---|---:|
| PROFITABILITY | 3.86 | | VOLATILITY | -1.40 |
| MT_MOMENTUM | 3.45 | | EARNINGS_YIELD | -1.76 |
| CROWDING | 3.41 | | SEC17 / CTY01 / CCY01 / MARKET | -5.00 each |
| GROWTH | 2.42 | | ST_MOMENTUM | -5.12 |
| SIZE | 1.75 | | DIVIDEND_YIELD | -5.94 |
| VALUE | 1.58 | | SHORT_INTEREST | -8.85 |
| EXCHANGE_RATE_SENS | 1.37 | | ESG_MOMENTUM | -13.85 |
| LEVERAGE | 1.31 | | MARKET_SENSITIVITY | 1.07 |
| LIQUIDITY | 0.70 | | | |

The same coverage caveat applies, and it matters even more here: assets 2 and 3 still have no loadings in AX_WW4_MH on this date, so **every number above is just asset 1's loadings × its −5mm active weight** — an exact mirror of the previous exposure table scaled by −½ (e.g. ESG_MOMENTUM 27.69 × −0.5 = −13.85). Your +5mm active in asset 2 and −5mm short in asset 3 contribute nothing.

So at face value you look underweight ESG momentum, short interest, and dividend yield, and overweight profitability and medium-term momentum versus the benchmark — but that's purely the underweight in asset 1 talking. With 10mm of your 15mm gross active money in uncovered names, I wouldn't put much weight on this picture until we either switch to a model covering assets 2 and 3 or use a date where they're in the universe.


## The show-stoppers


### Prompt 7 — 60s

> What's my flash PnL for today, and how will it compare to the official number tomorrow?

**Response:**

**Flash factor PnL for today (cob 2025-12-31): −$22.3k** on your 10/20/−5 book.

The damage comes from your two biggest tilts, both of which moved against you today:

| biggest losers | $k | | biggest winners | $k |
|---|---:|---|---|---:|
| SHORT_INTEREST | −14.9 | | PROFITABILITY | +4.0 |
| ESG_MOMENTUM | −8.7 | | CROWDING | +2.4 |
| DIVIDEND_YIELD | −3.6 | | SIZE | +1.3 |
| MT_MOMENTUM | −1.8 | | MARKET_SENSITIVITY | +1.1 |

Your +$17.7mm short-interest and +$27.7mm ESG-momentum exposures met small negative estimated returns (−8bp and −3bp respectively), while your underweights in profitability and crowding paid off as those factors also fell.

**How this compares to tomorrow's official number.** The flash uses the T0 estimate stream — factor returns estimated intraday by applying factor-mimicking-portfolio weights to today's asset returns — whereas tomorrow the vendor publishes official factor returns from its full cross-sectional regression. Your exposures are identical in both runs; the only thing that changes is the `fret` column. So tomorrow's official decomposition will have the same shape, and the difference per factor will be exactly `exposure × (official return − estimated return)`. Expect the big-exposure lines (ESG_MOMENTUM, SHORT_INTEREST) to move the most in dollar terms, since even a 1–2bp return revision on a ~$20–28mm exposure shifts that line by $2–6k.

Two caveats carried over from earlier: assets 2 and 3 are still uncovered in AX_WW4_MH on this date, so this is really asset 1's factor PnL; and the estimate stream covers 17 of your 20 exposed factors — SEC17, CTY01, and CCY01 have no T0 estimates, so those lines will appear only in the official run.


### Prompt 8 — 36s

> My momentum exposure moved over December — explain where the change is coming from.

**Response:**

Here's the December story for momentum. Both momentum factors moved, in opposite directions, and the drill-down shows it comes entirely from one place:

**Factor level (Dec 1 → Dec 31, $mm exposure):**
- **ST_MOMENTUM: +16.76 → +10.24, a drop of 6.52** — the bigger move
- **MT_MOMENTUM: −8.58 → −6.90, up 1.68** — a partial unwind of the negative tilt

**Asset level:** the by-asset breakdown attributes 100% of both changes to **asset 1**. That's mechanical at this point — assets 2 and 3 have no loadings in AX_WW4_MH, so asset 1 is the only name in the book the model sees.

And since `exposure_change` holds your positions constant over the window (your 10mm of asset 1 didn't change), the entire move is **re-loading, not trading**: Axioma revised asset 1's short-term momentum loading down from about 1.68 to 1.02 over the month, and its medium-term momentum loading up from −0.86 to −0.69. That pattern is what you'd expect from a stock whose strong recent run cooled off in December — the short-horizon momentum signal decayed quickly while the medium-horizon one drifted up slowly as older weak returns rolled out of its window.

Worth noting the momentum shift is not even the big December move in this book: ESG_MOMENTUM (+13.2), VOLATILITY (+13.1), DIVIDEND_YIELD (+12.2), and GROWTH (+11.4) all moved roughly twice as much, again all via asset 1's loadings. Your book's factor profile drifted quite a lot over the month without a single trade.


## Cache & workflow


### Prompt 9 — 99s

> Warm the cache for assets 1–3, run a few covered queries, and show me the hit/miss stats. Then persist the working set and prove a fresh session can start hot from it.

**Response:**

All done — warm, hits/misses, persist, and a provably-hot fresh session. Full sequence:

**1. Warm (session 1).** `fac.warm([1, 2, 3])` pulled the year-to-date working set into memory: 5,220 loading rows, 261 specific-risk rows, and 64,728 factor-return rows (the official stream is warmed for all factors, not just your assets). Counters start at zero.

**2. Covered queries → hits.** Four queries inside the warmed coverage (latest loadings and specific risk for assets 1–3, December factor returns, a mid-December loadings query) gave **4 hits, 0 misses** — served from memory, no store round-trips. As a control, I then asked for asset 4's specific risk, which was never warmed: **misses went to 1** and the request fell through to the store. Note the coverage is by scope, not rows — assets 2 and 3 have no data in this model, but queries about them still count as cache-covered because the warm proved the store has nothing for them.

**3. Persist.** `fac.save_cache()` wrote the working set to `/tmp/usercache/2025-12-31/AX_WW4_MH` — keyed by (as-of date, model), one parquet per dataset plus a coverage manifest and the dimension tables. The default base is the system temp dir so sets self-expire; set `$FACTOR_CACHE_DIR` for a durable location.

**4. Fresh session starts hot — provably.** In a brand-new process I unset `FACTOR_STORE_ROOT` and started with `ModelFacade.from_cache("AX_WW4_MH", root="/nonexistent/store")` — a root that would fail loudly on any store I/O. The session loaded the persisted set (same row counts), answered the same three queries with **3 hits, 0 misses**, and returned identical numbers (asset 1 specific risk 0.178984, matching earlier). One wrinkle: `from_cache` still requires *a* root string up front (`Store.open` validates it eagerly), even though it's only ever contacted on a cache miss — hence the dummy path rather than omitting it.

The morning-job pattern this enables: `warm(positions)` + `save_cache()` once after the store lands, then every later session `from_cache()` and runs the day's covered questions without opening a store connection at all. Remember the set is frozen at its key date (2025-12-31 here) — re-warm when your questions move past it.


## Guardrails (the fail-loud story)


### Prompt 10 — 39s

> Ask the BARRA_USE4_L model for estimated factor returns.

**Response:**

BARRA_USE4_L does carry a T0 estimate stream — here it is: 13 estimated factor returns, all for the latest date, 2025-12-31, flagged `type = T0_ESTIMATE`.

| factor | est. return (daily, bp) | | factor | est. return (daily, bp) |
|---|---:|---|---|---:|
| LIQUIDTY | +21.1 | | GROWTH | -1.2 |
| SENTMT | +15.8 | | DIVYLD | -2.4 |
| LEVERAGE | +6.2 | | SIZE | -7.3 |
| MOMENTUM | +5.7 | | SIZENL | -8.0 |
| BETA | +3.4 | | BTOP | -9.2 |
| RESVOL | +2.7 | | EARNYLD | -12.6 |
| COUNTRY | +1.0 | | | |

The strongest estimated moves today are liquidity (+21bp) and sentiment (+16bp) on the up side, and earnings yield (−13bp) and book-to-price (−9bp) on the down side — a risk-on, anti-value flavour to the day's estimates.

Two notes: the estimate stream covers the 13 style/market factors only, not the full 73-factor USE4 set (no industry estimates), and it exists only for the current date — that's the expected shape, since T0 estimates get superseded by official returns once the vendor's regression lands. Values are in the facade's canonical daily decimal, already converted from Barra's raw daily-percent convention.


### Prompt 11 — 35s

> Pass the string "2025-12-31" directly to the core Model's factor_loadings. What happens and why?

**Response:**

It raises immediately:

```
TypeError: as_of must be datetime.date, got 'str' — the core layer does not
coerce; use ModelFacade for string dates
```

**What happens:** the call never reaches the store — `Model.factor_loadings` type-checks its `as_of` argument up front and rejects anything that isn't a `datetime.date`, telling you exactly where to go instead.

**Why:** it's the strict/lenient split in the package's design. `fac.core` (the `Model` layer) is the strict contract: exact types in (`datetime.date` only), raw vendor values out, no coercion, no `'latest'` resolution, no unit scaling. All the ergonomics live one level up in `ModelFacade`, which accepts `date`, `'YYYY-MM-DD'`, or `'latest'` and normalizes to a `date` before delegating down. Keeping coercion out of the core means anything built directly on `Model` (the analytics package, the genv2 writer, tests) never has to wonder what a date argument might mean — a string slipping through can't silently parse as the wrong thing; it fails loudly at the boundary with a pointer to the right layer.

So the practical rule: strings are fine wherever you talk to the facade (`fac.get_factor_loadings("2025-12-31")` works, as we've been doing all session), but drop to `fac.core` and you owe it a real `datetime.date` — same rule `Portfolio.from_holdings` enforces on the analytics side.


## Verification


### Prompt 12 — 26s

> Run both selftests and summarize what they verify.

**Response:**

Both selftests pass cleanly: **modelfacade 13/13, analytics 7/7**.

**modelfacade selftest — the data layer.** It verifies the store and both facade layers end to end:

- *Store & conventions*: `list_models` sees the whole fleet; naming and unit-scale conventions hold (snake_case, discouraged names rejected).
- *Strict core*: rejects string/datetime dates and unknown models/factors (the behaviour we just demonstrated); serves sparse long-format loadings in raw vendor conventions over the correct date range.
- *Lenient facade*: string dates and `'latest'` resolve; wide pivot fills 0.0; vendor ids resolve via `asset_xref` both explicitly and auto-detected; output is in canonical units (specific risk, returns, covariance); the T0 estimate stream is reachable via the `type` column; `output="pandas"` converts at the return boundary only.
- *Cache*: pre-warm serves covered subsets from memory, persists to parquet and reloads across sessions, and `from_cache` cold-starts fully offline with zero store contact — the exact warm → persist → hot-start sequence we ran earlier.
- *Layer seam*: wrap/unwrap round-trips and `describe()` works.

**analytics selftest — the portfolio layer.** It verifies the stateless analytics on top:

- *Portfolio*: canonical construction with weights and the strict `datetime.date` guard; arithmetic (book − bench) aligns holdings and enforces matching dates.
- *Exposures*: value-weighted loadings match exact hand-computed cells, unwrapping the facade correctly.
- *PnL*: per-factor decomposition against the official return stream in $mm; flash PnL via `estimates=True`.
- *Drill-down*: `exposure_change` per-factor moves and by-asset attribution.
- *T0 estimation*: FMP weights × asset returns reproduces the stored estimate stream exactly — i.e. the flash numbers we quoted are internally consistent.

In short, everything this session leaned on — model listing, unit canonicalization, vendor-id resolution, the estimate stream, cache persistence, exposures, flash PnL, and the exposure-change drill-down — is covered by a passing check.
