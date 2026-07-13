"""Model facade runners.

    python -m modelfacade selftest                     # micro-store, no data needed
    python -m modelfacade demo --root DIR [--model ID] # against a real v2 store
"""

from __future__ import annotations

import argparse
import sys


def demo(root: str, model_id: str) -> None:
    from .facade import ModelFacade
    from .store import list_models

    print("models in store:")
    print(list_models(root))

    fac = ModelFacade.load(model_id, root)
    print(f"\ndescribe({model_id!r}):")
    for k, v in fac.describe().items():
        print(f"  {k}: {v}")

    print("\nget_factor_loadings(as_of='latest') — one line, wide:")
    print(fac.get_factor_loadings("latest").head())
    print("\nget_specific_risk('latest') — canonical annualized decimal:")
    print(fac.get_specific_risk("latest").head())
    print("\nget_factor_returns() — canonical daily decimal:")
    print(fac.get_factor_returns().head())


def main() -> int:
    ap = argparse.ArgumentParser(prog="modelfacade")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest")
    d = sub.add_parser("demo")
    d.add_argument("--root", required=True)
    d.add_argument("--model", default="AX_WW4_MH")
    args = ap.parse_args()

    if args.cmd == "selftest":
        from .selftest import main as selftest_main
        return selftest_main()
    demo(args.root, args.model)
    return 0


if __name__ == "__main__":
    sys.exit(main())
