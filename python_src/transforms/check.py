"""Transform consistency checks: row-count identities plus a value-level
roundtrip of sampled (date, asset) rows against the normalized store."""

from __future__ import annotations

from pathlib import Path

import duckdb

from generator.config import GeneratorConfig


def check(cfg: GeneratorConfig, normalized: Path, out_root: Path) -> int:
    con = duckdb.connect()
    failures = 0

    def result(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
        failures += 0 if ok else 1

    for m in cfg.models:
        cs = out_root / "wide_cs" / f"model_id={m.model_id}"
        ts = out_root / "wide_ts" / f"model_id={m.model_id}"
        sr = normalized / "specific_risk" / f"model_id={m.model_id}"

        n_cs, n_ts, n_sr = (
            con.execute(f"SELECT count(*) FROM read_parquet('{p}/**/*.parquet')").fetchone()[0]
            for p in (cs, ts, sr))
        result(f"{m.model_id}: wide_cs rows == specific_risk rows",
               n_cs == n_sr, f"{n_cs:,} vs {n_sr:,}")
        result(f"{m.model_id}: wide_ts rows == wide_cs rows",
               n_ts == n_cs, f"{n_ts:,} vs {n_cs:,}")

        # wide_ts must be sorted (asset_id, cob_date) within each bucket file
        n_unsorted = con.execute(f"""
            WITH t AS (
                SELECT bucket, asset_id, cob_date,
                       lag((asset_id, cob_date)) OVER
                           (PARTITION BY bucket ORDER BY file_row_number) AS prev
                FROM read_parquet('{ts}/**/*.parquet',
                                  hive_partitioning=true, file_row_number=true)
            )
            SELECT count(*) FROM t WHERE prev IS NOT NULL AND prev > (asset_id, cob_date)
        """).fetchone()[0]
        result(f"{m.model_id}: wide_ts sorted (asset_id, cob_date) in-bucket",
               n_unsorted == 0, f"{n_unsorted} inversions")

        # Value roundtrip on sampled keys: every normalized loading must appear
        # in the wide row, and every nonzero wide factor must exist in normalized.
        keys = con.execute(f"""
            SELECT cob_date, asset_id FROM read_parquet('{sr}/**/*.parquet')
            USING SAMPLE reservoir(5 ROWS) REPEATABLE ({cfg.global_seed})
        """).fetchall()
        mismatches = []
        for cob, aid in keys:
            wide = con.execute(f"""
                SELECT * FROM read_parquet('{cs}/**/*.parquet')
                WHERE cob_date = ? AND asset_id = ?
            """, [cob, aid]).fetch_arrow_table().to_pylist()[0]
            norm = dict(con.execute(f"""
                SELECT factor_id, value
                FROM read_parquet('{normalized}/factor_loading/model_id={m.model_id}/**/*.parquet')
                WHERE cob_date = ? AND asset_id = ?
            """, [cob, aid]).fetchall())
            for fid in m.factor_ids:
                if wide[fid] != norm.get(fid, 0.0):
                    mismatches.append(f"{cob}/{aid}/{fid}")
        result(f"{m.model_id}: sampled wide rows match normalized loadings",
               not mismatches, ", ".join(mismatches) or f"{len(keys)} keys checked")

    if failures:
        print(f"\n{failures} check(s) FAILED")
        return 1
    print("\nall transform checks passed")
    return 0
