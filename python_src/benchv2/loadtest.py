"""Concurrency sweep against the engine fleet: c = 1..10 simultaneous
clients, each firing a stream of warm queries round-robin across engines.
Reports per-concurrency p50/p95 latency and aggregate throughput.

Usage: ENGINE_URL=u1,u2,... python -m benchv2.loadtest --root s3://... \
           [--per-client 8] [--max-c 10]
"""
from __future__ import annotations

import argparse, itertools, json, os, statistics, threading, time
from pathlib import Path

import polars as pl
import pyarrow as pa
import pyarrow.ipc
import requests

from .queries import build

MIX = ["TS1", "CS2", "TS5", "CS4"]   # pruned, small-result: the interactive mix


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--params", default="data/v2bench12/params.json")
    ap.add_argument("--per-client", type=int, default=8)
    ap.add_argument("--max-c", type=int, default=10)
    ap.add_argument("--out", default="data/v2loadtest/results.json")
    args = ap.parse_args()
    prm = json.loads(Path(args.params).read_text())
    urls = [u.rstrip("/") for u in os.environ["ENGINE_URL"].split(",")]
    sqls = {q: build(q, "E_engine", args.root.rstrip("/"), prm["hundred"], prm["ts_asset"])
            for q in MIX}

    def fire(sess, url, q):
        t0 = time.perf_counter()
        rows = 0
        for sql in sqls[q]:
            r = sess.post(f"{url}/query", json={"sql": sql}, timeout=900)
            r.raise_for_status()
            rows += pl.from_arrow(pa.ipc.open_stream(r.content).read_all()).height
        return time.perf_counter() - t0

    # prewarm every engine on every mix query
    for u in urls:
        s = requests.Session()
        for q in MIX:
            fire(s, u, q)
    print(f"prewarmed {len(urls)} engines on {MIX}")

    results = {}
    for c in range(1, args.max_c + 1):
        lat, lock = [], threading.Lock()
        rr = itertools.cycle(urls)
        assign = [[next(rr) for _ in range(args.per_client)] for _ in range(c)]

        def client(ci):
            sess = requests.Session()
            mix = itertools.cycle(MIX)
            for url in assign[ci]:
                t = fire(sess, url, next(mix))
                with lock:
                    lat.append(t)

        t0 = time.perf_counter()
        threads = [threading.Thread(target=client, args=(i,)) for i in range(c)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        wall = time.perf_counter() - t0
        n = len(lat)
        results[c] = {"n": n, "wall_s": round(wall, 2),
                      "qps": round(n / wall, 2),
                      "p50_s": round(statistics.median(lat), 3),
                      "p95_s": round(sorted(lat)[max(0, int(n * .95) - 1)], 3)}
        print(f"c={c:2d}: {n} queries in {wall:5.1f}s -> {results[c]['qps']:5.2f} qps, "
              f"p50 {results[c]['p50_s']:.2f}s p95 {results[c]['p95_s']:.2f}s", flush=True)

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(results, indent=2))
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
