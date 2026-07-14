"""Cache-warming client: the scheduled job that makes every later session hot.

Reads a positions file, warms each requested model's working set (YTD
loadings + specific risk for the positions, all factor returns), and persists
it under the keyed layout <base>/usercache/<as_of>/<model_id>/ — where any
later ModelFacade.load(model).load_cache() picks it up. One model failing
does not stop the others; the exit code reports whether all succeeded.

    python warm_cache.py --demo                       # micro store, try it now
    python warm_cache.py --root DIR --model AX_WW4_MH \
                         --positions positions.txt    # real store

--demo is persistent: it builds (or reuses) the micro store under the repo's
data/demo/ and writes the working set to the real default cache base, so a
following  python usage_example.py  consumes exactly what this job produced —
the producer/consumer pipeline in miniature.

Positions file: one asset per line, internal integer ids or vendor ids
(not mixed), '#' comments allowed:

    # book: US long/short
    101
    102

As a cron job (warm at 06:15 before the risk team arrives; FACTOR_CACHE_DIR
makes the sets survive reboots):

    15 6 * * 1-5  FACTOR_STORE_ROOT=s3://bucket/v2 FACTOR_CACHE_DIR=~/.cache \
                  /path/venv/bin/python /path/python_src/warm_cache.py \
                  --model AX_WW4_MH --model BARRA_GEM_L \
                  --positions ~/positions.txt >> ~/warm_cache.log 2>&1
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from modelfacade import ModelFacade


def read_positions(path: str) -> list:
    """One asset per line: all internal int ids, or all vendor id strings."""
    out = []
    for line in Path(path).expanduser().read_text().splitlines():
        line = line.split("#")[0].strip()
        if line:
            out.append(int(line) if line.isdigit() else line)
    kinds = {isinstance(a, int) for a in out}
    if len(kinds) > 1:
        raise ValueError(f"{path}: mix of internal ids and vendor ids — "
                         "use one scheme per file")
    if not out:
        raise ValueError(f"{path}: no positions")
    return out


def warm_one(model_id: str, root: str | None, positions: list,
             as_of, sec_id_type) -> bool:
    """Warm and persist one model's working set; never raises (cron-safe)."""
    try:
        fac = ModelFacade.load(model_id, root)
        stats = fac.warm(positions, as_of=as_of, sec_id_type=sec_id_type)
        saved_to = fac.save_cache()
        rows = ", ".join(f"{k}={v}" for k, v in stats["rows"].items())
        print(f"OK    {model_id}: {len(positions)} positions, {rows}\n"
              f"      -> {saved_to}")
        return True
    except Exception as e:
        print(f"FAIL  {model_id}: {type(e).__name__}: {e}", file=sys.stderr)
        return False


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Warm and persist per-model user caches for a position list.")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--aws", action="store_true",
                     help="the project S3 store (needs AWS_FACTOR_READER_* keys in env)")
    grp.add_argument("--root",
                     help="store root (default: $FACTOR_STORE_ROOT)")
    ap.add_argument("--model", action="append", default=[],
                    help="model id to warm; repeat for several")
    ap.add_argument("--positions", help="file of asset ids, one per line")
    ap.add_argument("--as-of", default="latest",
                    help="COB date to warm up to (default: latest in store)")
    ap.add_argument("--sec-id-type", default=None,
                    help="scheme of vendor ids in the positions file "
                         "(e.g. AXIOMA, BARRA); omit to auto-detect")
    ap.add_argument("--demo", action="store_true",
                    help="run against a fabricated micro store")
    args = ap.parse_args()
    if args.aws:
        from modelfacade.store import AWS_ROOT
        args.root = AWS_ROOT

    if args.demo:
        # Persistent demo: micro store under the repo's data/demo/, working
        # set to the real default cache base — consumable afterwards by
        # usage_example.py (or any session's load_cache()).
        from modelfacade.selftest import MID, ensure_micro_store
        root = str(ensure_micro_store())
        models = [MID]
        positions = ["AX0000001", "AX0000002", "AX0000003"]
        print(f"demo: micro store at {root}\n"
              f"      cache to the default base — next, try:"
              f" python usage_example.py\n")
    else:
        if not args.model:
            ap.error("--model is required (repeat for several models)")
        if not args.positions:
            ap.error("--positions is required")
        root, models = args.root, args.model
        positions = read_positions(args.positions)

    results = [warm_one(m, root, positions, args.as_of, args.sec_id_type)
               for m in models]
    print(f"\n{sum(results)}/{len(results)} models warmed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
