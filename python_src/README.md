# python_src

Implementation of `../generator-spec.md` — synthetic multi-model equity factor
data generator writing the normalized Parquet store.

## Setup

```sh
python3 -m venv .venv            # from the repo root
.venv/bin/pip install -r python_src/requirements.txt
```

## Usage (from the repo root)

```sh
# Full 20-year, 2-model generation -> data/normalized/ (+ data/checkpoints/)
PYTHONPATH=python_src .venv/bin/python -m generator generate

# Regenerate a single year from its checkpoint
PYTHONPATH=python_src .venv/bin/python -m generator generate --years 2013

# Validation suite (spec §8); heavier optional checks behind flags
PYTHONPATH=python_src .venv/bin/python -m generator validate
PYTHONPATH=python_src .venv/bin/python -m generator validate --full --determinism 2016 --compression-control
```

Global config keys (seed, dates, universe sizing, output dirs) can be
overridden with `--config file.toml`; model definitions are pinned in
`generator/config.py`.

## Stage 2 transforms

```sh
# Build Transform A (wide_cs, date-major monthly partitions) then
# Transform B (wide_ts, asset-major buckets, re-sorted from A) -> data/transforms/
PYTHONPATH=python_src .venv/bin/python -m transforms build

# Row-count + value-roundtrip consistency checks
PYTHONPATH=python_src .venv/bin/python -m transforms check

# Daily-incremental cost probe (one date, isolated dir; plan: B's append is the awkward one)
PYTHONPATH=python_src .venv/bin/python -m transforms incremental [--date 2025-12-31]
```

Wall-times and output sizes land in `data/transforms/report.json` — the plan
treats these as the future daily-pipeline cost. Factor covariance is not
re-materialized; both transforms share the normalized date-sorted table.

## Stage 3 benchmark harness

```sh
# One-time: freeze query params, build native-DuckDB + DuckLake stores -> data/benchmark/
PYTHONPATH=python_src .venv/bin/python -m benchmark setup

# Full grid: 7 arms x 6 queries, 5 cold (fresh process, page cache evicted) + 10 warm
PYTHONPATH=python_src .venv/bin/python -m benchmark run
# Subsets: --arms normalized,wide_cs --queries CS1,TS1 --cold 3 --warm 5

# Re-summarize existing results.jsonl
PYTHONPATH=python_src .venv/bin/python -m benchmark report
```

Queries CS1–CS3 / TS1–TS3 run against every arm; the plan's X1 is the
off-diagonal of the grid. Metric is time to usable Polars DataFrame; bytes
scanned is the process rchar delta. Results: `data/benchmark/report.md`,
`summary.json`, `results.jsonl`.

Cross-environment comparison (Stage 4 — any number of labeled grids, first is
the baseline):

```sh
PYTHONPATH=python_src .venv/bin/python -m benchmark compare \
  local_nvme=data/benchmark/summary.json ec2_s3=data/results/ec2_s3/summary.json \
  --out benchmark-comparison-stage4.md
```

## Sample client — query the store on S3 via DuckLake

`sample_client.py` is the remote-research pattern end to end: download the
DuckLake catalog file from S3 (4.3 MiB — the entire metadata layer), attach it
read-only, run the six benchmark queries against the right layout for each,
and print first-run vs repeat timings.

**No AWS account or credentials required** — the demo prefixes
(`ducklake/*` + the catalog) are public-read, and the client falls back to
anonymous requests when `AWS_ACCESS_KEY_ID` is absent:

```sh
pip install -r requirements.txt
python python_src/sample_client.py
```

Representative output from a dev box outside AWS (eu-west-1 bucket):

```
query      first    repeat      rows
CS1       2993ms      32ms     2,949
TS1       2180ms      18ms     5,218
TS2       2523ms      17ms   130,500
```

The suite runs twice — once per risk model (Barra- and Axioma-style, each
with its own wide schema and factor taxonomy; model selection is table
selection, e.g. `cs_wide` vs `cs_wide_axioma`). First touch of each table
pays S3 latency; repeats run at local-NVMe speed because the catalog plans
everything locally — only data bytes cross the wire.
The catalog is cached at `~/.cache/factor-store/` and re-downloaded each run;
a shared-Postgres catalog (the deferred multi-reader arm) removes that step.

Warm repeats are served by DuckDB's external file cache: fetched S3 ranges
live in the buffer pool, capped by `memory_limit` (default 80% of RAM) with
LRU eviction — ample for the whole hot dataset. Inspect it with
`SELECT * FROM duckdb_external_file_cache()`; flush it with
`SET enable_external_file_cache = false` (instant, verified); it is
per-process, so every new session starts cold.

### cache_httpfs vs DuckLake — different layers, they stack

**DuckLake is a metadata layer.** A catalog (DuckDB file, or Postgres for
multi-user) recording tables, snapshots, data files, and per-file column
stats for Parquet in object storage. It removes *plan-time* round trips —
no bucket listing, no globbing, no footer reads (the ~30× warm penalty plain
Parquet pays from a remote client) — and adds transactional appends,
consistent snapshots, and time travel. It does not move data: every scan
still fetches from S3.

**cache_httpfs is a data layer.** A community extension that persists fetched
byte ranges to local disk, surviving across processes. It removes *repeated
data* round trips — the 2.7–5.4 s cold starts of a fresh session — at the
cost of disk management and staleness validation. It knows nothing about
tables; it just caches reads.

| Cost | Removed by |
|---|---|
| listing / glob / footer round trips | DuckLake (catalog plans locally) |
| re-fetching data within a session | DuckDB's built-in cache (above) |
| re-fetching data in a new session | cache_httpfs (disk, persistent) |
| the very first fetch of a range | neither — prefetch or sync |

They stack: DuckLake's data reads go through httpfs, so cache_httpfs caches
them transparently. Catalog + persistent range cache converges on the same
steady state as syncing the wide tables locally — lazily, per-range, instead
of ~2.7 GB upfront. Note DuckLake is a first-party DuckDB project with a
specified format; cache_httpfs is third-party community code — fine for a
research laptop, scrutinize before making it load-bearing.
