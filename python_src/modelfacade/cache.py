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

What a miss does is the cache's behaviour (Chris's extend-on-demand model,
2026-07-20): under EXTEND (the default) a miss is queried once, merged into
the working set, and covered thereafter — warm a core that answers most
questions, and let coverage grow to match actual usage instead of asking
users to predict it. A partly covered request fetches only the missing
cells (fetch-the-gap, on both axes): loaders are gap-aware, so extending a
warmed range by a week costs a week's rows, and asking about the warmed
book plus two new names costs the two names — not a re-load. Under STRICT a
miss falls through and the set is unchanged — serve only what was declared,
for reproducible sessions and bounded memory. Coverage is therefore a list
of segments per dataset: the warmed rectangle plus whatever extensions the
session accreted.

The cache identity includes the VIEW rows were loaded under: get()/put()
take view="official" by default, and any other view — a T0-estimate
stream, a PIT as-of — keys its own frames and coverage (dataset@view). A
point-in-time answer can therefore never be served a later republication,
and estimates can never be served as official: the identity requirement is
structural, not a convention callers must remember. Merges dedupe per cell
(cob_date / asset_id / factor_id) with freshly loaded rows winning, so a
republication re-fetched by a full-load path replaces the stale row
instead of sitting beside it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

import polars as pl

from conventions import ASSET_ID, COB_DATE, FACTOR_ID

# the default view: latest official data. Any other view (estimate stream,
# PIT as-of) keys its own frames — see the module docstring.
OFFICIAL_VIEW = "official"


class CacheBehaviour(Enum):
    """What a miss does to the working set.

    EXTEND (default): the loaded result is merged into the set and its
    coverage recorded, so the same question is answered from memory next
    time. STRICT: the set serves only declared coverage; a miss falls
    through and never mutates the set.
    """
    STRICT = "strict"
    EXTEND = "extend"


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
        self.coverage: dict[str, list[Coverage]] = {}   # always empty

    def get(self, dataset: str, start: date, end: date,
            assets: list[int] | None, loader,
            extendable: bool = True,
            view: str = OFFICIAL_VIEW) -> pl.DataFrame:
        return loader(start, end, assets)

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
    # distinct gap-signature groups fetched individually before degrading
    # to one full-request load (round trips have fixed latency; many small
    # queries cost more wall-clock than one slightly-fat one)
    MAX_FETCH_GROUPS = 3

    behaviour: CacheBehaviour = CacheBehaviour.EXTEND
    frames: dict[str, pl.DataFrame] = field(default_factory=dict)
    coverage: dict[str, list[Coverage]] = field(default_factory=dict)
    hits: int = 0
    misses: int = 0

    @staticmethod
    def _key(dataset: str, view: str) -> str:
        """The identity a frame is stored under. The official view keeps the
        bare dataset name (zero churn for the default path); any other view
        gets its own key — frames from different views can never mix."""
        return dataset if view == OFFICIAL_VIEW else f"{dataset}@{view}"

    def put(self, dataset: str, frame: pl.DataFrame, cov: Coverage,
            view: str = OFFICIAL_VIEW) -> None:
        """Declare `frame` as the dataset's working set for `view` —
        replaces any prior segments (a warm() resets the dataset,
        extensions included)."""
        key = self._key(dataset, view)
        self.frames[key] = frame
        self.coverage[key] = [cov]

    @staticmethod
    def _scope_covers(seg_assets: frozenset[int] | None,
                      assets: list[int] | None) -> bool:
        if seg_assets is None:
            return True
        return assets is not None and set(assets) <= seg_assets

    @classmethod
    def _seg_covers(cls, cov: Coverage, start: date, end: date,
                    assets: list[int] | None) -> bool:
        return (cov.start <= start and end <= cov.end
                and cls._scope_covers(cov.assets, assets))

    def covers(self, dataset: str, start: date, end: date,
               assets: list[int] | None,
               view: str = OFFICIAL_VIEW) -> bool:
        """True when the request fits entirely inside a single segment of
        the given view. Deliberately conservative: a request spanning two
        disjoint segments misses, gets loaded in full, and the merge
        coalesces the segments — self-healing rather than clever."""
        return any(self._seg_covers(c, start, end, assets)
                   for c in self.coverage.get(self._key(dataset, view), []))

    def get(self, dataset: str, start: date, end: date,
            assets: list[int] | None, loader,
            extendable: bool = True,
            view: str = OFFICIAL_VIEW) -> pl.DataFrame:
        """Serve from the working set when covered, else call loader.

        Args:
            dataset: Which warmed frame, e.g. ``"factor_loading"``.
            start: Requested range start (a single date is ``[d, d]``).
            end: Requested range end.
            assets: Requested asset scope; None = all assets.
            loader: Gap-aware callable — ``loader(s, e, a)`` returns the
                dataset's rows for dates [s, e] and assets ``a`` (None =
                all). Invoked only for uncovered cells — fall-through is
                always correct, coverage only decides where the answer
                came from.
            extendable: False marks a request whose loader does not return
                cache-shaped rows (e.g. factor-filtered) — it is served
                from coverage when possible but its result is never merged
                into the set.
            view: The publication view the request is for — part of the
                cache identity. A request for one view is never served
                another view's rows; each view extends its own frames.

        Returns:
            The covered slice of the working set, or the loaded rows.
            Under EXTEND (default) a miss is merged into the set, so the
            same question is answered from memory next time — and only the
            missing cells are fetched (fetch-the-gap, on both axes): a
            request extending a warmed range by a week loads a week; a
            book-plus-two-adds request loads the two adds. Missing assets
            are grouped by their gap ranges — one fetch per group's gaps —
            up to MAX_FETCH_GROUPS distinct groups; beyond that the whole
            request is loaded in one call, because many small round trips
            cost more than one slightly-fat one. Under STRICT the set is
            never mutated and a miss loads the full request.
        """
        key = self._key(dataset, view)
        extend = extendable and self.behaviour is CacheBehaviour.EXTEND
        if key in self.frames:
            if assets is None:
                gaps = self._date_gaps(key, start, end, None)
                if not gaps:                 # covered by the segment union
                    self.hits += 1
                    return self._serve(key, start, end, assets)
                if extend and gaps != [(start, end)]:
                    self.misses += 1
                    for gs, ge in gaps:      # fetch only the missing days
                        self._extend(key, loader(gs, ge, None),
                                     Coverage(gs, ge, None))
                    return self._serve(key, start, end, assets)
            else:
                missing = {a: tuple(self._date_gaps(key, start, end, [a]))
                           for a in assets}
                missing = {a: g for a, g in missing.items() if g}
                if not missing:              # every asset fully covered
                    self.hits += 1
                    return self._serve(key, start, end, assets)
                # group missing assets by their gap ranges: one fetch per
                # group's gaps, so only truly missing cells travel. Covers
                # pure date extension (every asset missing the same days),
                # pure asset addition (new assets, full range), and mixed
                # shapes — capped so a pathological spread of histories
                # degrades to one full-request load, not N round trips.
                groups: dict[tuple, list[int]] = {}
                for a, g in missing.items():
                    groups.setdefault(g, []).append(a)
                if extend and len(groups) <= self.MAX_FETCH_GROUPS:
                    self.misses += 1
                    for sig, ms in groups.items():
                        ms = sorted(ms)
                        for gs, ge in sig:
                            self._extend(key, loader(gs, ge, ms),
                                         Coverage(gs, ge, frozenset(ms)))
                    return self._serve(key, start, end, assets)
        self.misses += 1
        result = loader(start, end, assets)
        if extend:
            scope = None if assets is None else frozenset(assets)
            self._extend(key, result, Coverage(start, end, scope))
        return result

    def _serve(self, dataset: str, start: date, end: date,
               assets: list[int] | None) -> pl.DataFrame:
        frame = self.frames[dataset]
        expr = pl.col(COB_DATE).is_between(start, end)
        if assets is not None and ASSET_ID in frame.columns:
            expr &= pl.col(ASSET_ID).is_in(assets)
        return frame.filter(expr)

    def _date_gaps(self, dataset: str, start: date, end: date,
                   assets: list[int] | None) -> list[tuple[date, date]]:
        """Date sub-ranges of [start, end] not covered by any segment whose
        asset scope covers the request. [] = fully covered by the union;
        [(start, end)] = no usable coverage at all."""
        ivs = sorted(
            (max(c.start, start), min(c.end, end))
            for c in self.coverage.get(dataset, [])
            if c.start <= end and c.end >= start
            and self._scope_covers(c.assets, assets))
        gaps: list[tuple[date, date]] = []
        cursor = start
        for s, e in ivs:
            if s > cursor:
                gaps.append((cursor, s - timedelta(days=1)))
            if e >= cursor:
                cursor = e + timedelta(days=1)
            if cursor > end:
                break
        if cursor <= end:
            gaps.append((cursor, end))
        return gaps

    # ------------------------------------------------------------- extend
    @staticmethod
    def _mergeable(a: Coverage, b: Coverage) -> bool:
        """Same asset scope and date ranges that overlap or touch — the only
        case where two segments collapse into one rectangle losslessly."""
        if a.assets != b.assets:
            return False
        return (a.start <= b.end + timedelta(days=1)
                and b.start <= a.end + timedelta(days=1))

    def _extend(self, dataset: str, frame: pl.DataFrame,
                new: Coverage) -> None:
        """Merge a loaded result into the working set (EXTEND behaviour).
        Rows are concatenated and deduplicated; the new segment absorbs any
        existing segment with the same asset scope it overlaps or touches."""
        if dataset not in self.frames:
            self.frames[dataset] = frame
            self.coverage[dataset] = [new]
            return
        merged = pl.concat([self.frames[dataset], frame],
                           how="vertical_relaxed")
        # one row per cell, freshly loaded rows winning: a republication
        # re-fetched by a full-load path replaces the stale row instead of
        # sitting beside it (values may differ; full-row dedupe would keep
        # both)
        cell = [c for c in (COB_DATE, ASSET_ID, FACTOR_ID)
                if c in merged.columns]
        self.frames[dataset] = merged.unique(subset=cell or None,
                                             keep="last",
                                             maintain_order=True)
        remaining = []
        for seg in self.coverage[dataset]:
            if self._mergeable(seg, new):
                new = Coverage(min(seg.start, new.start),
                               max(seg.end, new.end), new.assets)
            else:
                remaining.append(seg)
        self.coverage[dataset] = remaining + [new]

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
            segs = self.coverage[name]
            manifest["datasets"][name] = {
                # overall bounds for readability; segments are authoritative
                "start": min(c.start for c in segs).isoformat(),
                "end": max(c.end for c in segs).isoformat(),
                "segments": [
                    {"start": c.start.isoformat(), "end": c.end.isoformat(),
                     "assets": sorted(c.assets) if c.assets is not None else None}
                    for c in segs],
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
            # pre-segment manifests carry a single rectangle
            segs = d.get("segments") or [d]
            cache.frames[name] = frame
            cache.coverage[name] = [
                Coverage(date.fromisoformat(s["start"]),
                         date.fromisoformat(s["end"]),
                         frozenset(s["assets"]) if s["assets"] is not None
                         else None)
                for s in segs]
        return cache, manifest["meta"]
