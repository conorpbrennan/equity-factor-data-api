"""Measurement worker — one process per cold iteration, one per warm batch.

Emits one JSON line per measurement on stdout. The timed region is
connection/ATTACH (cold only) + execute + Arrow fetch + Polars conversion:
"time to usable DataFrame". Bytes scanned = /proc/self/io rchar delta
(logical read bytes, includes page-cache hits — engine-agnostic)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import duckdb
import polars as pl

from .arms import ARM_IMPLS


def _rchar() -> int:
    for line in Path("/proc/self/io").read_text().splitlines():
        if line.startswith("rchar:"):
            return int(line.split()[1])
    raise RuntimeError("rchar not found in /proc/self/io")


def _fetch(con: duckdb.DuckDBPyConnection, sqls: list[str]) -> int:
    rows = 0
    for s in sqls:
        df = pl.from_arrow(con.execute(s).fetch_arrow_table())
        rows += df.height
    return rows


def run_worker(arm: str, query: str, mode: str, iterations: int,
               manifest_path: str) -> None:
    manifest = json.loads(Path(manifest_path).read_text())
    impl = ARM_IMPLS[arm]
    sqls = impl.sql(query, manifest)

    def emit(seconds: float, bytes_read: int, rows: int) -> None:
        print(json.dumps({"arm": arm, "query": query, "mode": mode,
                          "seconds": round(seconds, 4),
                          "bytes_read": bytes_read, "rows": rows}), flush=True)

    if mode == "cold":
        r0, t0 = _rchar(), time.perf_counter()
        con = impl.connect(manifest)
        rows = _fetch(con, sqls)
        emit(time.perf_counter() - t0, _rchar() - r0, rows)
        return

    con = impl.connect(manifest)
    _fetch(con, sqls)  # warmup, unmeasured
    for _ in range(iterations):
        r0, t0 = _rchar(), time.perf_counter()
        rows = _fetch(con, sqls)
        emit(time.perf_counter() - t0, _rchar() - r0, rows)
