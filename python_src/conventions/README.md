# conventions — firm-wide data conventions as an importable library

## Why this project

Inconsistent conventions create friction at every seam: time is lost renaming
columns, converting units, and remembering which spelling of `as_of` this
particular library wanted — and each inconsistency is a place for subtle bugs
to hide. The fix is not a document that says what the conventions are; those
already exist and disagree with each other. The fix is a library that *is* the
conventions, so new code imports them and legacy data gets sanitized at the
point where it enters the system.

## Proposed change

One small package, four modules, each one row of the straw-man table:

| module | decision | straw man |
|---|---|---|
| `columns` | column-name case + shared string constants | snake_case; one constant per column name, imported everywhere, never re-typed |
| `identifiers` | security identifier schema | `sec_id` + `sec_id_type` for single-scheme frames; explicit `sec_id_<scheme>` columns for multi-scheme; typed enum for the scheme values |
| `columns` (streams) | publication streams | `type = OFFICIAL \| T0_ESTIMATE` as ordinary rows; orthogonal to `version_id` (restatements — an official row can itself be restated); the stream toggle is an equality filter, never a join |
| `units` | scale and frequency per quantity | returns daily decimal; vol annualized decimal; covariance annualized decimal²; money in millions USD — all conversions executable via `scale_to_canonical()` |
| `signatures` | function argument naming | `as_of` / `start` / `end` / `assets` / `factors` / `model`; a `DISCOURAGED` map of spellings seen in the wild |

The strategy is target-plus-adapter, not forced migration: anything new
imports the constants; anything legacy is handled by the mini toolkit
(`snake_case`, `rename_snake`) at the boundary.

## What the scaffold shows

- The consistency has more value than any particular choice — every choice
  here is a straw man and swapping one is a one-line change that every
  importer picks up.
- Unit conversions collapse to a lookup table of multipliers because every
  convention pair seen so far is multiplicative — so canonicalization is one
  `with_columns` on the way out of a store, not per-call logic.
- Identifier mapping rules that need stating once, in code comments where
  they're used: vendor↔vendor is many-to-many, mappings must be dated, and
  vendor→vendor never chains silently.

## Open questions

- Who ratifies? These need a single owner with the standing to make them
  stick; a library can enforce spellings, not adoption.
- Is `sec_id_type` a free string or the closed enum? Closed here (drift-proof)
  but that makes adding a scheme a code change.
- Do canonical units belong to the core layer or the user layer? (See
  modelfacade/README.md — this scaffold puts conversion at the user layer and
  keeps core raw; the reverse is defensible.)
