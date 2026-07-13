"""Analytics runners.

    python -m analytics selftest    # micro-store contract checks, no data
"""

from __future__ import annotations

import argparse
import sys


def main() -> int:
    ap = argparse.ArgumentParser(prog="analytics")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest")
    ap.parse_args()
    from .selftest import main as selftest_main
    return selftest_main()


if __name__ == "__main__":
    sys.exit(main())
