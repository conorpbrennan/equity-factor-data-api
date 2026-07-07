"""CLI:

    python -m transforms build [--normalized DIR] [--output DIR] [--buckets N]
    python -m transforms incremental [--date YYYY-MM-DD] ...
    python -m transforms check [--normalized DIR] [--output DIR]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from generator.config import load_config

from . import DEFAULT_BUCKETS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="transforms")
    parser.add_argument("--config", help="TOML overriding generator global config")
    parser.add_argument("--normalized", default=None,
                        help="normalized store (default: config output_dir)")
    parser.add_argument("--output", default="data/transforms")
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="build Transform A then B for all models")
    p_build.add_argument("--buckets", type=int, default=DEFAULT_BUCKETS)

    p_inc = sub.add_parser("incremental",
                           help="measure the one-date daily append cost (isolated probe)")
    p_inc.add_argument("--date", help="COB date (default: calendar end)")
    p_inc.add_argument("--buckets", type=int, default=DEFAULT_BUCKETS)

    sub.add_parser("check", help="row-count and value-roundtrip consistency checks")

    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    normalized = Path(args.normalized) if args.normalized else Path(cfg.output_dir)
    out_root = Path(args.output)

    if args.command == "build":
        from .build import build
        build(cfg, normalized, out_root, buckets=args.buckets)
        return 0
    if args.command == "incremental":
        from .build import incremental_probe
        from generator.trading_calendar import trading_days
        cob = args.date or str(trading_days(cfg.start_date, cfg.end_date)[-1])
        incremental_probe(cfg, normalized, out_root, cob, buckets=args.buckets)
        return 0
    from .check import check
    return check(cfg, normalized, out_root)


if __name__ == "__main__":
    sys.exit(main())
