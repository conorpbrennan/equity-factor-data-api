"""CLI:

    python -m benchmark setup [--model M] [--normalized DIR] [--transforms DIR] [--output DIR]
    python -m benchmark run [--arms a,b] [--queries CS1,TS1] [--cold 5] [--warm 10]
    python -m benchmark report          # re-summarize existing results.jsonl
    python -m benchmark worker ...      # internal
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import ARMS, QUERIES


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmark")
    parser.add_argument("--output", default="data/benchmark")
    sub = parser.add_subparsers(dest="command", required=True)

    p_setup = sub.add_parser("setup", help="resolve params, build native + ducklake stores")
    p_setup.add_argument("--config", help="TOML overriding generator global config")
    p_setup.add_argument("--model", default="BARRA_USE4_L")
    p_setup.add_argument("--normalized", default=None)
    p_setup.add_argument("--transforms", default="data/transforms")

    p_s3 = sub.add_parser("setup-s3", help="build the S3 manifest + S3-backed ducklake")
    p_s3.add_argument("--bucket", required=True, help="s3://bucket[/prefix]")
    p_s3.add_argument("--local", default="data/benchmark",
                      help="local benchmark dir to copy frozen params from")
    p_s3.add_argument("--region", default="eu-west-1")

    p_run = sub.add_parser("run", help="run the benchmark grid")
    p_run.add_argument("--arms", default=",".join(ARMS))
    p_run.add_argument("--queries", default=",".join(QUERIES))
    p_run.add_argument("--cold", type=int, default=5)
    p_run.add_argument("--warm", type=int, default=10)

    sub.add_parser("report", help="re-summarize results.jsonl")

    p_cmp = sub.add_parser("compare", help="cross-environment comparison report")
    p_cmp.add_argument("grids", nargs="+", metavar="LABEL=summary.json",
                       help="first grid is the baseline")
    p_cmp.add_argument("--out", default=None)

    p_w = sub.add_parser("worker")
    p_w.add_argument("--arm", required=True)
    p_w.add_argument("--query", required=True)
    p_w.add_argument("--mode", choices=("cold", "warm"), required=True)
    p_w.add_argument("--iterations", type=int, default=1)
    p_w.add_argument("--manifest", required=True)

    args = parser.parse_args(argv)
    bench_dir = Path(args.output)

    if args.command == "setup":
        from generator.config import load_config
        from .setup_stores import setup
        cfg = load_config(args.config)
        setup(cfg, args.model,
              Path(args.normalized) if args.normalized else Path(cfg.output_dir),
              Path(args.transforms), bench_dir)
        return 0
    if args.command == "setup-s3":
        from .setup_stores import setup_s3
        setup_s3(args.bucket, Path(args.local), bench_dir, region=args.region)
        return 0
    if args.command == "run":
        from .runner import run
        run(bench_dir, args.arms.split(","), args.queries.split(","),
            args.cold, args.warm)
        return 0
    if args.command == "report":
        from .runner import summarize
        summarize(bench_dir)
        return 0
    if args.command == "compare":
        from .compare import compare
        pairs = [(g.split("=", 1)[0], Path(g.split("=", 1)[1])) for g in args.grids]
        compare(pairs, Path(args.out) if args.out else None)
        return 0
    from .worker import run_worker
    run_worker(args.arm, args.query, args.mode, args.iterations, args.manifest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
