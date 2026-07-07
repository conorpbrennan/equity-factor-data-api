"""Seed scheme (generator-spec.md §3).

Counter-based Philox streams keyed by SHA-256(global_seed | model_id | component).
Per-date draws set the counter to date_index << 64, giving each date 2^64
blocks of stream space — no overlap between dates regardless of draw volume.
One-time (static) draws use their own component name so they never collide
with a date stream.
"""

from __future__ import annotations

import hashlib

import numpy as np


def stream(global_seed: int, model_id: str, component: str, date_index: int = 0) -> np.random.Generator:
    digest = hashlib.sha256(f"{global_seed}|{model_id}|{component}".encode()).digest()
    key = int.from_bytes(digest[:16], "little")
    bitgen = np.random.Philox(counter=date_index << 64, key=key)
    return np.random.Generator(bitgen)


def round_sig(x: np.ndarray, p: int = 7) -> np.ndarray:
    """Round to p significant digits (vendor file precision, spec §4.9)."""
    x = np.asarray(x, dtype=np.float64)
    out = x.copy()
    nz = x != 0.0
    mag = np.floor(np.log10(np.abs(x[nz])))
    scale = np.power(10.0, p - 1 - mag)
    out[nz] = np.round(x[nz] * scale) / scale
    return out
