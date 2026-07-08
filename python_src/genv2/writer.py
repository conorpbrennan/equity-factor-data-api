"""V2 table builders — like v1's but with explicit version columns, the two
new datasets, chunked (monthly) files for global-scale models, and S3-native
output: any destination may be a local path or an s3:// URI, written directly
(no local staging + sync)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from pyarrow import fs as pafs

from generator.writer import write_parquet as _write_local   # v1 writer, unchanged


def is_s3(root) -> bool:
    return str(root).startswith("s3://")


def write_any(table: pa.Table, dest: str | Path, cfg) -> None:
    """Write one parquet file to a local path or an s3:// URI."""
    dest = str(dest)
    if is_s3(dest):
        filesystem, path = pafs.FileSystem.from_uri(dest)
        pq.write_table(table, path, filesystem=filesystem,
                       compression=cfg.compression,
                       compression_level=cfg.compression_level,
                       row_group_size=cfg.row_group_size,
                       write_statistics=True)
    else:
        _write_local(table, Path(dest), cfg)


def write_chunk(table: pa.Table, root, name: str, model_id: str,
                year: int, chunk: int, cfg) -> None:
    dest = f"{root}/{name}/model_id={model_id}/year={year}/data_{chunk:02d}.parquet"
    write_any(table, dest, cfg)


def _fcol(seq: np.ndarray, factor_ids: list[str]) -> pa.Array:
    return pa.DictionaryArray.from_arrays(pa.array(seq, type=pa.int16()),
                                          pa.array(factor_ids, type=pa.string()))


def loading_table(dates, slots, seq, vals, ver, factor_ids) -> pa.Table:
    return pa.table({
        "cob_date": pa.array(dates),
        "asset_id": pa.array(slots + 1, type=pa.int32()),
        "factor_id": _fcol(seq, factor_ids),
        "value": pa.array(vals, type=pa.float64()),
        "version_id": pa.array(ver, type=pa.int16()),
    })


def covariance_table(dates, s1, s2, vals, factor_ids) -> pa.Table:
    n = len(vals)
    return pa.table({
        "cob_date": pa.array(dates),
        "factor_id_1": _fcol(s1, factor_ids),
        "factor_id_2": _fcol(s2, factor_ids),
        "value": pa.array(vals, type=pa.float64()),
        "version_id": pa.array(np.ones(n, np.int16)),
    })


def srisk_table(dates, slots, vals, ver) -> pa.Table:
    return pa.table({
        "cob_date": pa.array(dates),
        "asset_id": pa.array(slots + 1, type=pa.int32()),
        "value": pa.array(vals, type=pa.float64()),
        "version_id": pa.array(ver, type=pa.int16()),
    })


def membership_table(dates, slots, estu) -> pa.Table:
    return pa.table({
        "cob_date": pa.array(dates),
        "asset_id": pa.array(slots + 1, type=pa.int32()),
        "estimation_universe_flag": pa.array(estu, type=pa.bool_()),
    })


def freturn_table(dates, seq, vals, factor_ids) -> pa.Table:
    n = len(vals)
    return pa.table({
        "cob_date": pa.array(dates),
        "factor_id": _fcol(seq, factor_ids),
        "value": pa.array(vals, type=pa.float64()),
        "version_id": pa.array(np.ones(n, np.int16)),
    })


def fmp_table(dates, seq, slots, vals, factor_ids) -> pa.Table:
    n = len(vals)
    return pa.table({
        "cob_date": pa.array(dates),
        "factor_id": _fcol(seq, factor_ids),
        "asset_id": pa.array(slots + 1, type=pa.int32()),
        "weight": pa.array(vals, type=pa.float64()),
        "version_id": pa.array(np.ones(n, np.int16)),
    })
