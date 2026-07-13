# Prompts to paste into Claude Code

Launch `claude` from the repo root (so the `factor-data` skill is
discovered), then paste these one at a time. Each should make Claude write
and run a snippet against `modelfacade`/`analytics` and answer from the
resulting frames — no real data needed, the demo micro store builds itself.

Ordered as an escalating demo: discovery → data → analytics → the
show-stoppers → the guardrails.

## Discovery

> What factor models are in the store, and what units does each vendor
> publish in?

*Expect:* `list_models()` — two models, their cov/srisk/return conventions.

> Describe the AX_TEST1_MH model — styles, date range, raw units.

*Expect:* `describe()` output summarized.

## Data pulls

> Show me the latest factor loadings for AX_TEST1_MH, wide format.

*Expect:* 6 assets × one column per factor, one-hots visible as 0/1.

> Get specific risk for AX0000001 and AX0000002 as of the latest date.

*Expect:* vendor ids resolved via asset_xref; values in annualized decimal
(0.29, 0.30) with a note they were stored as ann_vol_pct.

## Analytics

> I hold 10mm of asset 1, 20mm of asset 2, and I'm short 5mm of asset 3.
> What are my factor exposures today?

*Expect:* a Portfolio built, `exposures()` sorted by magnitude; the short
netting IND01 down.

> Same book against a benchmark of 15mm each in assets 1 and 2 — what are
> my active exposures?

*Expect:* `book - bench`, active column with the market bet netting to ~0.

## The show-stoppers

> What's my flash PnL for today, and how will it compare to the official
> number tomorrow?

*Expect:* `pnl_decomposition` twice — `estimates=True` vs default — side by
side with totals; an explanation that only the return stream differs.

> My momentum exposure moved over the stored history — explain where the
> change is coming from.

*Expect:* `exposure_change` per factor, then `by_asset=True` on the top
mover, then a one-sentence verdict naming the driving asset. (This is the
"drill into risk changes" scenario.)

## Cache & workflow

> Warm the cache for assets 1–3, run a few covered queries, and show me the
> hit/miss stats. Then persist the working set and prove a fresh session
> can start hot from it.

*Expect:* `warm()` → hits with zero misses → `save_cache()` /
`load_cache()` round trip under `usercache/<date>/<model>/`.

## Guardrails (the fail-loud story)

> Ask the BARRA_TEST1_L model for estimated factor returns.

*Expect:* a refusal with the exact error — that model's store predates the
type column, so it has no estimate stream; Claude should report that, not
substitute official numbers.

> Pass the string "2025-01-15" directly to the core Model's
> factor_loadings. What happens and why?

*Expect:* the TypeError, and the two-layer explanation (core never coerces;
the facade is where leniency lives).

## Verification

> Run both selftests and summarize what they verify.

*Expect:* 12/12 and 6/6 with a one-line-per-check summary.
