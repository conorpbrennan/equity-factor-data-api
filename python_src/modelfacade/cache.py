"""User-layer cache: pre-warm an expected working set, serve subsets from it.

The design premise (not query-result caching): most of a user's questions hit
a predictable working set — e.g. year-to-date data for the assets they hold in
the model they care about. warm() loads that set once; get() serves any
subset request from it and falls through to the loader for anything outside.
Lives strictly at the facade layer so leniency never leaks into core.

The working set can be persisted (to_disk/from_disk): one parquet file per
dataset plus a manifest.json carrying the coverage declarations, so a set
warmed once — by an earlier session, or a scheduled morning job — is reusable
across sessions. Persistence freezes the data as of the save; the caller
decides when a saved set is too old and re-warms.

Caching is OFF by default (project decision, 2026-07-15): a facade built
without cache= gets NoCache, where every request falls through to the store.
Opting in — and every staleness trade-off that comes with it — is the
caller's explicit choice: pass cache=UserCache(), or adopt a persisted set
via load_cache()/from_cache().
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import polars as pl

from conventions import ASSET_ID, COB_DATE


@dataclass
class Coverage:
    start: date
    end: date
    assets: frozenset[int] | None    # None = all assets


class NoCache:
    """The off-by-default state: nothing is retained, every get() falls
    through to its loader. warm() refuses (put raises) — a working set with
    nowhere to live is a silent no-op the user would misread as warmed."""

    def __init__(self) -> None:
        self.coverage: dict[str, Coverage] = {}   # always empty

    def get(self, dataset: str, start: date, end: date,
            assets: list[int] | None, loader) -> pl.DataFrame:
        return loader()

    def put(self, dataset: str, frame: pl.DataFrame, cov: Coverage) -> None:
        raise ValueError(
            "caching is off by default — pass cache=UserCache() when "
            "constructing the facade (ModelFacade.load(..., "
            "cache=UserCache())) before warm()")

    @property
    def stats(self) -> dict:
        return {"hits": 0, "misses": 0, "rows": {}}

    def clear(self) -> None:
        pass


@dataclass
class UserCache:
    frames: dict[str, pl.DataFrame] = field(default_factory=dict)
    coverage: dict[str, Coverage] = field(default_factory=dict)
    hits: int = 0
    misses: int = 0

    def put(self, dataset: str, frame: pl.DataFrame, cov: Coverage) -> None:
        self.frames[dataset] = frame
        self.coverage[dataset] = cov

    def covers(self, dataset: str, start: date, end: date,
               assets: list[int] | None) -> bool:
        cov = self.coverage.get(dataset)
        if cov is None or not (cov.start <= start and end <= cov.end):
            return False
        if cov.assets is None:
            return True
        return assets is not None and set(assets) <= cov.assets

    def get(self, dataset: str, start: date, end: date,
            assets: list[int] | None, loader) -> pl.DataFrame:
        """Serve from the warmed frame when covered, else call loader.

        Args:
            dataset: Which warmed frame, e.g. ``"factor_loading"``.
            start: Requested range start (a single date is ``[d, d]``).
            end: Requested range end.
            assets: Requested asset scope; None = all assets.
            loader: Zero-arg callable hitting the store; invoked only on
                a miss — fall-through is always correct, coverage only
                decides where the answer came from.

        Returns:
            The covered slice of the warmed frame, or ``loader()``.
        """
        if self.covers(dataset, start, end, assets):
            self.hits += 1
            frame = self.frames[dataset]
            expr = pl.col(COB_DATE).is_between(start, end)
            if assets is not None and ASSET_ID in frame.columns:
                expr &= pl.col(ASSET_ID).is_in(assets)
            return frame.filter(expr)
        self.misses += 1
        return loader()

    @property
    def stats(self) -> dict:
        rows = {k: len(v) for k, v in self.frames.items()}
        return {"hits": self.hits, "misses": self.misses, "rows": rows}

    def clear(self) -> None:
        """Invalidate the whole working set (e.g. on a known restatement):
        every frame and coverage declaration is dropped, so all requests
        fall through to the store until the next warm(). Session counters
        are kept — they describe the session, not the working set."""
        self.frames.clear()
        self.coverage.clear()

    # ------------------------------------------------------------ persistence
    def to_disk(self, path: str | Path, meta: dict | None = None) -> Path:
        """Write the working set under `path`: one parquet per dataset plus
        manifest.json (coverage + caller-supplied meta, e.g. model_id)."""
        path = Path(path).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        saved_at = datetime.now(timezone.utc).isoformat()
        manifest: dict = {"meta": {**(meta or {}), "saved_at": saved_at},
                          "datasets": {}}
        for name, frame in self.frames.items():
            frame.write_parquet(path / f"{name}.parquet")
            cov = self.coverage[name]
            manifest["datasets"][name] = {
                "start": cov.start.isoformat(), "end": cov.end.isoformat(),
                "assets": sorted(cov.assets) if cov.assets is not None else None,
                "rows": len(frame),
            }
        (path / "manifest.json").write_text(json.dumps(manifest, indent=2))
        return path

    @classmethod
    def from_disk(cls, path: str | Path) -> tuple["UserCache", dict]:
        """Rebuild a cache from to_disk() output; returns (cache, meta).
        Counters start at zero — stats describe the new session."""
        path = Path(path).expanduser()
        manifest = json.loads((path / "manifest.json").read_text())
        cache = cls()
        for name, d in manifest["datasets"].items():
            frame = pl.read_parquet(path / f"{name}.parquet")
            assets = frozenset(d["assets"]) if d["assets"] is not None else None
            cache.put(name, frame,
                      Coverage(date.fromisoformat(d["start"]),
                               date.fromisoformat(d["end"]), assets))
        return cache, manifest["meta"]
