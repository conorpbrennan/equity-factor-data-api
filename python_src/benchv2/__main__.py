"""Three-way benchmark runner.

    python -m benchv2 params --root DIR              # resolve + freeze params
    python -m benchv2 run --root DIR [--iters-file]  # full grid -> results
    python -m benchv2 worker ...                     # internal (one cell)

Cold = fresh worker process, arm-relevant dirs fadvise-evicted first, timing
includes connect. Warm = repeats in-process after one unmeasured warmup.
DuckDB per worker: DUCK_MEM/DUCK_THREADS (defaults 40GB/14 — no-spill rule).
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

from . import ARMS, QUERIES, SHARED_QUERIES
from .queries import MODEL, build, paths


def _duck(root: str):
    import duckdb
    con = duckdb.connect()
    con.execute(f"SET threads = {os.environ.get('DUCK_THREADS', 14)}")
    con.execute(f"SET memory_limit = '{os.environ.get('DUCK_MEM', '40GB')}'")
    tmp = os.environ.get("DUCK_TMP")
    if tmp:
        Path(f"{tmp}/pid{os.getpid()}").mkdir(parents=True, exist_ok=True)
        con.execute(f"SET temp_directory = '{tmp}/pid{os.getpid()}'")
    return con


def resolve_params(root: str) -> dict:
    """100 full-history assets covered by the model, and a TS1 asset."""
    con = _duck(root)
    p = paths(root)
    rows = con.execute(f"""
        SELECT s.asset_id FROM (
            SELECT asset_id FROM read_parquet('{p["srisk"]}/year=2006/*.parquet')
            WHERE cob_date = (SELECT min(cob_date) FROM read_parquet('{p["srisk"]}/year=2006/*.parquet'))
        ) s
        JOIN (
            SELECT asset_id FROM read_parquet('{p["srisk"]}/year=2025/*.parquet')
            WHERE cob_date = (SELECT max(cob_date) FROM read_parquet('{p["srisk"]}/year=2025/*.parquet'))
        ) e USING (asset_id)
        ORDER BY s.asset_id LIMIT 100
    """).fetchall()
    ids = [r[0] for r in rows]
    assert len(ids) == 100, f"only {len(ids)} full-history assets"
    return {"model": MODEL, "hundred": ids, "ts_asset": ids[9]}


def worker(root: str, arm: str, qid: str, mode: str, iters: int, params_file: str):
    import polars as pl
    prm = json.loads(Path(params_file).read_text())

    def run_once(con):
        rows = 0
        for sql in build(qid, arm, root, prm["hundred"], prm["ts_asset"]):
            df = pl.from_arrow(con.execute(sql).to_arrow_table())
            rows += df.height
        return rows

    if mode == "cold":
        t0 = time.perf_counter()
        con = _duck(root)
        rows = run_once(con)
        print(json.dumps({"arm": arm, "query": qid, "mode": "cold",
                          "seconds": round(time.perf_counter() - t0, 4), "rows": rows}))
        return
    con = _duck(root)
    run_once(con)
    for _ in range(iters):
        t0 = time.perf_counter()
        rows = run_once(con)
        print(json.dumps({"arm": arm, "query": qid, "mode": "warm",
                          "seconds": round(time.perf_counter() - t0, 4), "rows": rows}), flush=True)


ARM_DIRS = {
    "A_permodel": ("wide_cs", "wide_ts"),
    "B_generic": ("gen_cs", "gen_ts"),
    "C_normalized": ("loading", "srisk"),
}


def evict(root: str, arm: str):
    p = paths(root)
    dirs = [p[k] for k in ARM_DIRS[arm]] + [p["mem"], p["cov"], p["fret"], p["fmp"]]
    for d in dirs:
        base = Path(d)
        if not base.exists():
            continue
        for f in base.rglob("*.parquet"):
            try:
                fd = os.open(f, os.O_RDONLY)
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
                os.close(fd)
            except OSError:
                pass


def _p(vals, q):
    s = sorted(vals)
    return s[min(len(s) - 1, round(q * (len(s) - 1)))]


def run(root: str, out_dir: Path, cold_iters: int, warm_iters: int, slow_scale: dict):
    out_dir.mkdir(parents=True, exist_ok=True)
    params_file = out_dir / "params.json"
    if not params_file.exists():
        params_file.write_text(json.dumps(resolve_params(root)))
    results = []

    def cell(arm, qid):
        scale = slow_scale.get((arm, qid), 1.0)
        n_cold = max(1, round(cold_iters * scale))
        n_warm = max(1, round(warm_iters * scale))
        for _ in range(n_cold):
            evict(root, arm)
            results.extend(_spawn(root, arm, qid, "cold", 1, params_file))
        results.extend(_spawn(root, arm, qid, "warm", n_warm, params_file))
        c = [r["seconds"] for r in results if r["arm"] == arm and r["query"] == qid and r["mode"] == "cold"]
        w = [r["seconds"] for r in results if r["arm"] == arm and r["query"] == qid and r["mode"] == "warm"]
        print(f"{arm:14s} {qid:6s} cold p50 {_p(c, .5):8.2f}s | warm p50 {_p(w, .5):8.3f}s", flush=True)

    for qid in QUERIES:
        for arm in ARMS:
            cell(arm, qid)
    for qid in SHARED_QUERIES:      # same physical path for every arm
        cell("A_permodel", qid)

    (out_dir / "results.jsonl").write_text("\n".join(json.dumps(r) for r in results) + "\n")
    summary = {}
    for r in results:
        k = f"{r['arm']}/{r['query']}/{r['mode']}"
        summary.setdefault(k, []).append(r["seconds"])
    (out_dir / "summary.json").write_text(json.dumps(
        {k: {"n": len(v), "p50_s": round(_p(v, .5), 4), "p95_s": round(_p(v, .95), 4),
             "rows": next(r["rows"] for r in results
                          if f"{r['arm']}/{r['query']}/{r['mode']}" == k)}
         for k, v in sorted(summary.items())}, indent=2))
    print(f"-> {out_dir}/summary.json")


def _spawn(root, arm, qid, mode, iters, params_file):
    proc = subprocess.run(
        [sys.executable, "-m", "benchv2", "worker", "--root", root, "--arm", arm,
         "--query", qid, "--mode", mode, "--iters", str(iters),
         "--params", str(params_file)],
        capture_output=True, text=True, timeout=7200,
        env={**os.environ, "PYTHONPATH": "python_src"})
    if proc.returncode != 0:
        raise RuntimeError(f"{arm}/{qid}/{mode}: {proc.stderr[-800:]}")
    return [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="benchv2")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("params", "run", "worker"):
        pp = sub.add_parser(name)
        pp.add_argument("--root", required=True)
        if name == "run":
            pp.add_argument("--out", default="data/v2bench")
            pp.add_argument("--cold", type=int, default=3)
            pp.add_argument("--warm", type=int, default=5)
        if name == "worker":
            pp.add_argument("--arm", required=True)
            pp.add_argument("--query", required=True)
            pp.add_argument("--mode", required=True)
            pp.add_argument("--iters", type=int, default=1)
            pp.add_argument("--params", required=True)
    args = ap.parse_args(argv)

    if args.cmd == "params":
        print(json.dumps(resolve_params(args.root))[:400])
        return 0
    if args.cmd == "worker":
        worker(args.root, args.arm, args.query, args.mode, args.iters, args.params)
        return 0
    # C-arm TS1/CS1 scan the multi-100GB normalized store per iteration: 1 cold, 2 warm
    slow = {("C_normalized", "TS1"): 0.4, ("C_normalized", "CS1"): 0.4,
            ("C_normalized", "CHAIN1"): 0.4, ("C_normalized", "CHAIN2"): 0.4}
    run(args.root, Path(args.out), args.cold, args.warm, slow)
    return 0


if __name__ == "__main__":
    sys.exit(main())
