"""Shared example plumbing: imports root, args, store selection.

Each example calls setup() and gets back (root, model_id) — the demo micro
store by default (built on first use), or --root/--model from the command
line for a real v2 store. Kept tiny on purpose: everything worth reading
lives in the examples themselves.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # python_src


def setup(description: str) -> tuple[str, str]:
    ap = argparse.ArgumentParser(description=description)
    ap.add_argument("--root", help="v2 store root (default: demo micro store)")
    ap.add_argument("--model", default=None, help="model id")
    args = ap.parse_args()
    if args.root:
        return args.root, args.model or "AX_WW4_MH"
    from modelfacade.selftest import MID, ensure_micro_store
    return str(ensure_micro_store()), args.model or MID
