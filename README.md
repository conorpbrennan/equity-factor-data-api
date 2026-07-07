# Equity Factor Data API — storage layout benchmark

Synthetic Barra/Axioma-style equity factor model data (20 years × ~3,000
assets × 2 risk models), generated deterministically from seed, materialized
into query-optimized layouts, and benchmarked across storage engines and
environments — local NVMe, EC2, and S3 via DuckLake.

## Try it in two minutes — no AWS account needed

The demo dataset lives in a public-read S3 bucket. The sample client downloads
the DuckLake catalog (4.3 MiB), attaches it in DuckDB, and runs the six
benchmark queries anonymously:

```sh
pip install duckdb polars boto3
python python_src/sample_client.py
```

Expected output (from a machine outside AWS, eu-west-1 bucket):

```
query      first    repeat      rows  description
CS1       3118ms      34ms     2,949  full cross-section, one date, all factors
CS2       1133ms      14ms     2,635  one date, 5 factors, estimation universe only
CS3       1325ms      26ms     5,650  cross-section + covariance + specific risk (B·F·Bᵀ + D)
TS1       3031ms      18ms     5,218  one asset, 20 years, all factors
TS2       2976ms      18ms   130,500  100 assets, 5 years, 3 factors (mixed)
TS3       6328ms      15ms     5,218  one covariance pair, 20 years
```

First touch of each table pays S3 latency; repeats run at local-disk speed
over the public internet, because the DuckLake catalog plans every scan
locally — only data bytes cross the wire.

## What's here

| Path | What it is |
|---|---|
| `factor-model-benchmark-plan.md` | The plan: design decisions, schemas, benchmark arms, AWS stages |
| `generator-spec.md` | Pinned generator spec: parameters, algorithms, seed scheme, DDL |
| `benchmark-results.md` | Stage 3 results: 7 storage arms × 6 queries on local NVMe |
| `benchmark-comparison-stage4.md` / `.html` | Stage 4: the same grid across four environments (dev NVMe, EC2 NVMe, EC2→S3, dev→S3) |
| `python_src/generator/` | Data generator — byte-identical output from seed, with validation suite |
| `python_src/transforms/` | DuckDB SQL transforms: date-major and asset-major wide layouts |
| `python_src/benchmark/` | Benchmark harness: cold/warm protocol, p50/p95, bytes scanned, cross-env comparison |
| `python_src/sample_client.py` | The two-minute demo above |

## Headline findings

- Dual materialization pays: each wide layout wins its native queries by
  **5–48×** over the normalized store; querying across the wrong layout costs
  ~12× (bytes scanned: megabytes vs the full ~1.4 GB store).
- Moving compute to EC2 changed nothing structural (flat ~1.7× core-count tax);
  moving data to in-region S3 cost **+9%** warm.
- From a dev box over the public internet, **DuckLake warm queries match local
  NVMe (14–27 ms)** while plain Parquet-over-S3 pays ~30× in metadata round
  trips. Cold full-store scans cost ~50 s — sync, or don't scan remotely.

## Reproducing the full pipeline

Generation (~1 min), transforms (~5 min), and the benchmark grids are all
driven from `python_src/` — commands, options, and per-stage notes in
[`python_src/README.md`](python_src/README.md). The dataset is deterministic:
same config ⇒ byte-identical Parquet, so results are reproducible from a
clone with no data download.
