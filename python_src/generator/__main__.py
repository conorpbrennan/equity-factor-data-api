"""CLI (generator-spec.md §9).

    python -m generator generate [--config cfg.toml] [--years 2013 | 2010-2015] [--output DIR]
    python -m generator validate [--config cfg.toml] [--data DIR] [--full]
                                 [--determinism YEAR] [--compression-control]
"""

from __future__ import annotations

import argparse
import sys

from .config import load_config


def _parse_years(spec: str) -> list[int]:
    if "-" in spec:
        lo, hi = spec.split("-", 1)
        return list(range(int(lo), int(hi) + 1))
    return [int(spec)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="generator")
    sub = parser.add_subparsers(dest="command", required=True)

    p_gen = sub.add_parser("generate", help="generate the normalized Parquet store")
    p_gen.add_argument("--config", help="TOML file overriding global config keys")
    p_gen.add_argument("--years", help="e.g. 2013 or 2010-2015 (default: full calendar); "
                                       "resumes from the prior year's checkpoint")
    p_gen.add_argument("--output", help="override output_dir")
    p_gen.add_argument("--quiet", action="store_true")

    p_val = sub.add_parser("validate", help="run the validation suite (spec §8)")
    p_val.add_argument("--config", help="TOML file overriding global config keys")
    p_val.add_argument("--data", help="data dir to validate (default: config output_dir)")
    p_val.add_argument("--full", action="store_true",
                       help="PSD-check every date instead of one sample year")
    p_val.add_argument("--determinism", type=int, metavar="YEAR",
                       help="regenerate YEAR and require byte-identical output")
    p_val.add_argument("--compression-control", action="store_true",
                       help="compare compression vs a dense i.i.d. noise control")

    args = parser.parse_args(argv)
    cfg = load_config(args.config)

    if args.command == "generate":
        from .generate import generate
        generate(cfg, years=_parse_years(args.years) if args.years else None,
                 output_dir=args.output, quiet=args.quiet)
        return 0

    from .validate import run_validation
    return run_validation(cfg, data_dir=args.data, full=args.full,
                          determinism_year=args.determinism,
                          compression_control=args.compression_control)


if __name__ == "__main__":
    sys.exit(main())
