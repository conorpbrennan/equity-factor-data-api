"""The DRILL (generator-spec-v2 §3–4): time 'a new model arrives' end to end
per storage strategy. Scores wall-time, steps, bytes added, objects touched.

Usage: python -m benchv2.drill --root DIR
Models: whatever fleet.TIERS['drill'] lists (their normalized data must exist —
that generation is the shared 'data arrival' cost, timed separately).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import duckdb

from genv2.fleet import FLEET, TIERS, make_config
from genv2.transforms import (BUCKETS, COPY_OPTS, MAX_SLOTS, _bucketize,
                              _connect, _periods, _pivot_sql, _slot_select,
                              write_slot_map_and_views)


def _du(path: str) -> float:
    p = Path(path)
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 2**30 \
        if p.exists() else 0.0


def drill(root: str) -> dict:
    cfg = make_config("drill", output_dir=f"{root}/normalized",
                      checkpoint_dir=f"{root}/checkpoints")
    years = list(range(cfg.start_date.year, cfg.end_date.year + 1))
    out: dict = {}

    for m in cfg.models:
        r: dict = {}
        con = _connect(root)

        # ---- strategy A: per-model wide tables (cs pivot + ts buckets)
        t0 = time.perf_counter()
        cs = f"{root}/transforms_a/wide_cs/model_id={m.model_id}"
        Path(cs).mkdir(parents=True, exist_ok=True)
        for lo, hi in _periods(m, years):
            con.execute(f"""
                COPY (SELECT strftime(cob_date, '%Y-%m') AS year_month, *
                      FROM ({_pivot_sql(m, f"{root}/normalized", lo, hi)})
                      ORDER BY cob_date, asset_id)
                TO '{cs}' ({COPY_OPTS}, PARTITION_BY (year_month), APPEND)""")
        ts = f"{root}/transforms_a/wide_ts/model_id={m.model_id}"
        _bucketize(con, f"{cs}/**/*.parquet", ts, years,
                   select="* EXCLUDE (year_month)")
        r["A"] = {"seconds": round(time.perf_counter() - t0, 1),
                  "steps": "1 command (config-generated DDL; pivot + buckets)",
                  "gb_added": round(_du(cs) + _du(ts), 2),
                  "objects_added": "2 new tables (dirs) per layout pair",
                  "existing_touched": 0}

        # ---- strategy B: normalized -> generic slots DIRECTLY + map/views
        t0 = time.perf_counter()
        gcs = f"{root}/transforms_b/generic_cs/model_id={m.model_id}"
        Path(gcs).mkdir(parents=True, exist_ok=True)
        for lo, hi in _periods(m, years):
            con.execute(f"""
                COPY (SELECT strftime(cob_date, '%Y-%m') AS year_month,
                             cob_date, asset_id, specific_risk, {_slot_select(m)}
                      FROM ({_pivot_sql(m, f"{root}/normalized", lo, hi)})
                      ORDER BY cob_date, asset_id)
                TO '{gcs}' ({COPY_OPTS}, PARTITION_BY (year_month), APPEND)""")
        gts = f"{root}/transforms_b/generic_ts/model_id={m.model_id}"
        _bucketize(con, f"{gcs}/**/*.parquet", gts, years,
                   select="* EXCLUDE (year_month)")
        t_map = time.perf_counter()
        write_slot_map_and_views(cfg, f"{root}/transforms_b", quiet=True)
        map_s = time.perf_counter() - t_map
        r["B"] = {"seconds": round(time.perf_counter() - t0, 1),
                  "map_views_seconds": round(map_s, 2),
                  "steps": "1 command (rows into existing table's new partition) "
                           "+ slot-map rows + 2 views",
                  "gb_added": round(_du(gcs) + _du(gts), 2),
                  "objects_added": "0 new tables; +2 views, +N slot-map rows",
                  "existing_touched": "2 metadata files (slot map, views.sql)"}

        # ---- strategy C: nothing to do; price = the first query
        t0 = time.perf_counter()
        lo, hi = "2016-01-01", "2016-12-31"
        n = con.execute(f"""SELECT count(*) FROM ({_pivot_sql(m, f"{root}/normalized", lo, hi)})
                            WHERE cob_date = DATE '2016-06-15'""").fetchone()[0]
        r["C"] = {"seconds": 0.0,
                  "steps": "none — queryable immediately",
                  "gb_added": 0.0, "objects_added": "none", "existing_touched": 0,
                  "first_query_seconds": round(time.perf_counter() - t0, 2),
                  "first_query_rows": n}

        # ---- verify all surfaces return the same cross-section
        counts = {}
        counts["A"] = con.execute(
            f"SELECT count(*) FROM read_parquet('{cs}/**/*.parquet', hive_partitioning=true) "
            f"WHERE year_month = '2016-06' AND cob_date = DATE '2016-06-15'").fetchone()[0]
        counts["B"] = con.execute(
            f"SELECT count(*) FROM read_parquet('{gcs}/**/*.parquet', hive_partitioning=true) "
            f"WHERE year_month = '2016-06' AND cob_date = DATE '2016-06-15'").fetchone()[0]
        counts["C"] = n
        r["parity"] = counts
        r["parity_ok"] = len(set(counts.values())) == 1
        out[m.model_id] = r
        print(json.dumps({m.model_id: r}, indent=1), flush=True)
        con.close()
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", default="data/v2drill/results.json")
    args = ap.parse_args()
    res = drill(args.root.rstrip("/"))
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(res, indent=2))
    print(f"-> {args.out}")
