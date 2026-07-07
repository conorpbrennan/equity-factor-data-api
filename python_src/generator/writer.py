"""Parquet output (generator-spec.md §6): hive layout, zstd, stats on."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from .config import GeneratorConfig


def write_parquet(table: pa.Table, path: Path, cfg: GeneratorConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        table, path,
        compression=cfg.compression,
        compression_level=cfg.compression_level,
        row_group_size=cfg.row_group_size,
        write_statistics=True,
    )


def partition_path(root: Path, table: str, model_id: str, year: int) -> Path:
    return root / table / f"model_id={model_id}" / f"year={year}" / "data.parquet"


def _version_col(n: int) -> pa.Array:
    return pa.array(np.ones(n, dtype=np.int16))


def _factor_col(seq: np.ndarray, factor_ids: list[str]) -> pa.Array:
    return pa.DictionaryArray.from_arrays(
        pa.array(seq, type=pa.int16()), pa.array(factor_ids, type=pa.string()))


def loading_table(dates_rep: np.ndarray, slot_idx: np.ndarray, factor_seq: np.ndarray,
                  values: np.ndarray, factor_ids: list[str]) -> pa.Table:
    return pa.table({
        "cob_date": pa.array(dates_rep),
        "asset_id": pa.array(slot_idx + 1, type=pa.int32()),
        "factor_id": _factor_col(factor_seq, factor_ids),
        "value": pa.array(values, type=pa.float64()),
        "version_id": _version_col(len(values)),
    })


def covariance_table(dates_rep: np.ndarray, seq1: np.ndarray, seq2: np.ndarray,
                     values: np.ndarray, factor_ids: list[str]) -> pa.Table:
    return pa.table({
        "cob_date": pa.array(dates_rep),
        "factor_id_1": _factor_col(seq1, factor_ids),
        "factor_id_2": _factor_col(seq2, factor_ids),
        "value": pa.array(values, type=pa.float64()),
        "version_id": _version_col(len(values)),
    })


def specific_risk_table(dates_rep: np.ndarray, slot_idx: np.ndarray,
                        values: np.ndarray) -> pa.Table:
    return pa.table({
        "cob_date": pa.array(dates_rep),
        "asset_id": pa.array(slot_idx + 1, type=pa.int32()),
        "value": pa.array(values, type=pa.float64()),
        "version_id": _version_col(len(values)),
    })


def membership_table(dates_rep: np.ndarray, slot_idx: np.ndarray,
                     estu: np.ndarray) -> pa.Table:
    return pa.table({
        "cob_date": pa.array(dates_rep),
        "asset_id": pa.array(slot_idx + 1, type=pa.int32()),
        "estimation_universe_flag": pa.array(estu, type=pa.bool_()),
    })
