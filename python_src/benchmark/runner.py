"""Orchestrates the benchmark grid: cold iterations in fresh subprocesses with
page cache evicted first, warm iterations batched in one subprocess. Writes
results.jsonl and a p50/p95 summary (summary.json + report.md)."""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

from . import ARMS, QUERIES


def _evict_page_cache(roots: list[Path]) -> None:
    """Best-effort cold state without root: fadvise DONTNEED every data file."""
    for root in roots:
        if not root.exists():
            continue
        for f in root.rglob("*"):
            if f.is_file():
                try:
                    fd = os.open(f, os.O_RDONLY)
                    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
                    os.close(fd)
                except OSError:
                    pass


def _spawn(arm: str, query: str, mode: str, iterations: int,
           manifest_path: Path) -> list[dict]:
    proc = subprocess.run(
        [sys.executable, "-m", "benchmark", "worker", "--arm", arm,
         "--query", query, "--mode", mode, "--iterations", str(iterations),
         "--manifest", str(manifest_path)],
        capture_output=True, text=True, timeout=900)
    if proc.returncode != 0:
        raise RuntimeError(f"worker {arm}/{query}/{mode} failed:\n{proc.stderr}")
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def _p(vals: list[float], q: float) -> float:
    s = sorted(vals)
    return s[min(len(s) - 1, round(q * (len(s) - 1)))]


def run(bench_dir: Path, arms: list[str], queries: list[str],
        cold_iters: int, warm_iters: int) -> None:
    manifest_path = bench_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    data_roots = [Path(manifest["normalized"]), Path(manifest["transforms"]),
                  bench_dir]

    available = manifest.get("arms_available")
    results: list[dict] = []
    t_start = time.perf_counter()
    for arm in arms:
        if available is not None and arm not in available:
            print(f"skipping {arm} (not available in this manifest)")
            continue
        if arm.startswith("ducklake") and not manifest["ducklake_available"]:
            print(f"skipping {arm} (ducklake unavailable)")
            continue
        for query in queries:
            for _ in range(cold_iters):
                _evict_page_cache(data_roots)
                results += _spawn(arm, query, "cold", 1, manifest_path)
            results += _spawn(arm, query, "warm", warm_iters, manifest_path)
            cold = [r["seconds"] for r in results
                    if r["arm"] == arm and r["query"] == query and r["mode"] == "cold"]
            warm = [r["seconds"] for r in results
                    if r["arm"] == arm and r["query"] == query and r["mode"] == "warm"]
            print(f"{arm:12s} {query}: cold p50 {_p(cold, .5):7.3f}s, "
                  f"warm p50 {_p(warm, .5):7.3f}s", flush=True)

    out = bench_dir / "results.jsonl"
    out.write_text("\n".join(json.dumps(r) for r in results) + "\n")
    print(f"\n{len(results)} measurements in {time.perf_counter() - t_start:.0f}s "
          f"-> {out}")
    summarize(bench_dir)


def summarize(bench_dir: Path) -> None:
    results = [json.loads(line) for line in
               (bench_dir / "results.jsonl").read_text().splitlines()]
    manifest = json.loads((bench_dir / "manifest.json").read_text())

    cells: dict[tuple, dict] = {}
    for (arm, query, mode) in {(r["arm"], r["query"], r["mode"]) for r in results}:
        sel = [r for r in results
               if (r["arm"], r["query"], r["mode"]) == (arm, query, mode)]
        secs = [r["seconds"] for r in sel]
        cells[(arm, query, mode)] = {
            "n": len(sel),
            "p50_s": round(_p(secs, .5), 4), "p95_s": round(_p(secs, .95), 4),
            "mb_read": round(statistics.median(r["bytes_read"] for r in sel) / 2**20, 1),
            "rows": sel[0]["rows"],
        }

    # Cross-arm row-count parity: every arm must return the same result size.
    parity = {}
    for query in QUERIES:
        counts = {arm: cells[(arm, query, "warm")]["rows"]
                  for arm in ARMS if (arm, query, "warm") in cells}
        parity[query] = counts
        if len(set(counts.values())) > 1:
            print(f"WARNING: row-count mismatch on {query}: {counts}")

    arms_present = [a for a in ARMS if any(k[0] == a for k in cells)]
    lines = [
        "# Benchmark results", "",
        f"Model `{manifest['model_id']}` — metric is time to usable Polars "
        "DataFrame; bytes = logical read volume (rchar). "
        "X1 is the off-diagonal: TS\\* on cs-layouts, CS\\* on ts-layouts.", "",
    ]
    for mode, title in (("warm", "Warm (in-process repeat)"),
                        ("cold", "Cold (fresh process, page cache evicted)")):
        lines += [f"## {title}", "",
                  "| query | " + " | ".join(arms_present) + " |",
                  "|---" * (len(arms_present) + 1) + "|"]
        for query in QUERIES:
            row = [query]
            for arm in arms_present:
                c = cells.get((arm, query, mode))
                row.append(f"{c['p50_s']:.3f}s (p95 {c['p95_s']:.3f}) "
                           f"{c['mb_read']:.0f}MB" if c else "—")
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")
    lines += ["## Store sizes", ""]
    if manifest.get("native_bytes"):
        lines.append(f"- native.duckdb: {manifest['native_bytes'] / 2**30:.2f} GiB "
                     f"(built in {manifest['native_seconds']}s)")
    if manifest.get("ducklake_bytes"):
        lines.append(f"- ducklake: {manifest['ducklake_bytes'] / 2**30:.2f} GiB "
                     f"(built in {manifest['ducklake_seconds']}s)")
    (bench_dir / "report.md").write_text("\n".join(lines) + "\n")

    (bench_dir / "summary.json").write_text(json.dumps(
        {"cells": {f"{a}/{q}/{m}": v for (a, q, m), v in sorted(cells.items())},
         "row_parity": parity}, indent=2))
    print(f"summary -> {bench_dir / 'report.md'}")
