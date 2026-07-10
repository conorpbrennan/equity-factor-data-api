"""CLI:

    python -m genv2 generate --tier dev|full [--years 2006-2007] [--output DIR]
    python -m genv2 validate --tier dev|full [--output DIR]
"""

from __future__ import annotations

import argparse
import dataclasses
import sys


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="genv2")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_plan = sub.add_parser("plan", help="no-spill lane plan for a box")
    p_plan.add_argument("--tier", default="full")
    p_plan.add_argument("--mem-gb", type=float, required=True)
    p_plan.add_argument("--threads", type=int, required=True)
    for name in ("generate", "validate", "transforms"):
        p = sub.add_parser(name)
        p.add_argument("--tier", default="dev")  # any key of fleet.TIERS
        p.add_argument("--output", default=None, help="override output root")
        p.add_argument("--checkpoints", default=None)
        if name == "generate":
            p.add_argument("--years", default=None, help="e.g. 2006 or 2006-2008")
            p.add_argument("--quiet", action="store_true")
            p.add_argument("--models", default=None,
                           help="comma-separated model_id subset (parallel lanes)")
            p.add_argument("--skip-dims", action="store_true")
            p.add_argument("--dims-only", action="store_true")
        if name == "transforms":
            p.add_argument("--strategy", choices=("a", "b", "both", "map"), default="both")
            p.add_argument("--out-root", default="data/v2")
            p.add_argument("--models", default=None,
                           help="comma-separated model_id subset (parallel lanes)")
    args = ap.parse_args(argv)

    from .fleet import make_config
    cfg = make_config(args.tier)

    if args.cmd == "plan":
        from .transforms import est_transform_mem_gb, plan_lanes
        for i, wave in enumerate(plan_lanes(cfg.models, args.mem_gb, args.threads)):
            print(f"wave {i}: " + "  ".join(
                f"{mid}(mem={mem}GB,thr={thr})" for mid, mem, thr in wave))
        return 0
    over = {}
    if args.output:
        over["output_dir"] = args.output
    if args.checkpoints:
        over["checkpoint_dir"] = args.checkpoints
    if over:
        cfg = dataclasses.replace(cfg, **over)

    if args.cmd == "generate":
        years = None
        if args.years:
            lo, _, hi = args.years.partition("-")
            years = list(range(int(lo), int(hi or lo) + 1))
        if args.models:
            wanted = args.models.split(",")
            cfg = dataclasses.replace(
                cfg, models=tuple(m for m in cfg.models if m.model_id in wanted))
        from .generate import generate
        generate(cfg, years=years, quiet=args.quiet,
                 skip_dims=args.skip_dims, dims_only=args.dims_only)
        return 0
    if args.cmd == "transforms":
        if args.models:
            wanted = args.models.split(",")
            cfg = dataclasses.replace(
                cfg, models=tuple(m for m in cfg.models if m.model_id in wanted))
        from .transforms import build
        build(cfg, cfg.output_dir, args.out_root, args.strategy)
        return 0
    from .validate import run_validation
    return run_validation(cfg)


if __name__ == "__main__":
    sys.exit(main())
