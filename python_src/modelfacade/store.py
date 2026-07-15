"""Read-only store handle: connection, dimension tables, per-model paths.

Layout is the genv2 store (generator-spec-v2.md): dimension parquet at the
root, hive-partitioned facts underneath, optional transforms_b fast path.
Local paths and s3:// both work (AWS_FACTOR_READER_* env keys, region
eu-west-1 default).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from typing import Sequence

import duckdb
import polars as pl

from conventions import (ASSET_ID, COB_DATE, FACTOR_ID, MODEL_ID, TYPE,
                         VERSION_ID)

# the project's S3 store — what every CLI's --aws flag resolves to, defined
# once so the bucket name never gets re-typed
AWS_ROOT = "s3://equity-factor-data-651406457779/v2"

_DIMS = ("model_master", "factor_master", "asset_master", "asset_xref")

_FACTS = ("asset_return", "factor_loading", "factor_covariance", "specific_risk",
          "universe_membership", "factor_return", "fmp")


@dataclass
class Store:
    root: str
    threads: int = int(os.environ.get("DUCK_THREADS", 4))
    memory_limit: str = os.environ.get("DUCK_MEM", "4GB")
    _con: duckdb.DuckDBPyConnection | None = field(default=None, repr=False)
    _dims: dict[str, pl.DataFrame] = field(default_factory=dict, repr=False)
    _engine: str | None = field(default=None, repr=False)
    _compute_checked: bool = field(default=False, repr=False)
    _cols: dict[tuple[str, str, str], bool] = field(default_factory=dict,
                                                    repr=False)

    def __post_init__(self) -> None:
        self.root = self.root.rstrip("/")

    @classmethod
    def open(cls, root: str | None = None) -> "Store":
        root = root or os.environ.get("FACTOR_STORE_ROOT")
        if not root:
            raise ValueError("no store: pass root= or set FACTOR_STORE_ROOT")
        return cls(root=root)

    # ------------------------------------------------------------ connection
    def _ensure_compute(self) -> None:
        """Ensure the in-region query engine is up before first S3 data access.

        Active only when the store is s3:// AND the jump service is
        configured (JUMP_SERVICE_URL + JUMP_SERVICE_TOKEN in the
        environment) — local stores and unconfigured environments skip it
        entirely. Blocking: ~1 min if the box has to launch, <1s when it
        is already alive. Fails loudly if the service is configured but
        cannot deliver a box — a half-provisioned session would only fail
        later and less clearly. On success, queries route to the engine
        on the box (sql() below) instead of scanning S3 locally.
        """
        if self._compute_checked:
            return
        if not self.root.startswith("s3://"):
            return
        if not (os.environ.get("JUMP_SERVICE_URL")
                and os.environ.get("JUMP_SERVICE_TOKEN")):
            return
        from querybox import ensure
        box = ensure()
        if not box:
            raise RuntimeError(
                f"jump service could not provide a query box: {box.detail}")
        url = f"http://{box.public_ip}:8080"
        if self._engine_alive(url):
            self._engine = url
        else:
            # box is up but the engine port doesn't answer (service down or
            # security group): degrade to local scans, loudly — queries stay
            # correct, only the in-region execution is lost
            import sys
            print(f"WARNING: query engine unreachable at {url}; "
                  "falling back to local S3 scans", file=sys.stderr)
        self._compute_checked = True

    @staticmethod
    def _engine_alive(url: str) -> bool:
        import urllib.request
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=3) as resp:
                return resp.status == 200
        except OSError:
            return False

    @property
    def con(self) -> duckdb.DuckDBPyConnection:
        if self._con is None:
            self._ensure_compute()
            con = duckdb.connect()
            con.execute(f"SET threads = {self.threads}")
            con.execute(f"SET memory_limit = '{self.memory_limit}'")
            if self.root.startswith("s3://"):
                con.execute("INSTALL httpfs; LOAD httpfs;")
                con.execute("SET http_retries = 8;")
                region = os.environ.get("AWS_REGION", "eu-west-1")
                # reads authenticate with the dedicated read-only factor-store
                # keys, never with general-purpose AWS credentials
                key_id = os.environ.get("AWS_FACTOR_READER_ACCESS_KEY_ID")
                secret = os.environ.get("AWS_FACTOR_READER_SECRET_ACCESS_KEY")
                if key_id and secret:
                    con.execute(f"""CREATE SECRET (TYPE s3,
                        KEY_ID '{key_id}',
                        SECRET '{secret}',
                        REGION '{region}')""")
                else:
                    # no credentials: empty-config secret pins the region and
                    # requests go unsigned — works on public-read prefixes
                    con.execute(f"CREATE SECRET (TYPE s3, PROVIDER config, "
                                f"REGION '{region}')")
            self._con = con
        return self._con

    def sql(self, query: str) -> pl.DataFrame:
        self._ensure_compute()
        if self._engine:
            table = self._engine_query(query)
        else:
            # fetch_arrow_table, not .arrow(): the latter returns a
            # RecordBatchReader (duckdb >= 1.5) whose zero-batch case polars
            # refuses ("Must pass schema, or at least one RecordBatch")
            table = self.con.execute(query).fetch_arrow_table()
        if table.num_rows == 0:
            table = table.schema.empty_table()   # empty frame, schema intact
        return pl.from_arrow(table)

    def _engine_query(self, query: str):
        """POST the query to the in-region engine; Arrow IPC stream back."""
        import json
        import urllib.error
        import urllib.request

        import pyarrow as pa
        import pyarrow.ipc  # noqa: F401  (registers the ipc submodule)

        req = urllib.request.Request(
            f"{self._engine}/query", method="POST",
            data=json.dumps({"sql": query}).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=900) as resp:
                body = resp.read()
        except urllib.error.HTTPError as e:
            # the engine surfaces DuckDB errors as 400 + detail; re-raise
            # with the text so a bad query reads the same as it does locally
            raise RuntimeError(
                "query engine error "
                f"{e.code}: {e.read().decode(errors='replace')[:2000]}") from None
        return pa.ipc.open_stream(body).read_all()

    # ------------------------------------------- DataSource protocol reads
    # Query composition lives here: hive pruning, WHERE assembly, layout
    # columns. Callers (core.Model) ask in dates/assets/factors and never
    # see SQL or partition artifacts.
    @staticmethod
    def _in(col: str, values: Sequence) -> str:
        items = ", ".join(f"'{v}'" if isinstance(v, str) else str(v)
                          for v in values)
        return f"AND {col} IN ({items}) " if values else ""

    def read_fact(self, fact: str, model_id: str, *,
                  start: date, end: date,
                  assets: Sequence[int] | None = None,
                  factors: Sequence[str] | None = None,
                  version: int = 1,
                  pub_type: str | None = None) -> pl.DataFrame:
        """Rows of one fact for one model over [start, end], filtered.

        Single-date reads are start == end; DuckDB prunes the year
        partitions from the BETWEEN identically to an equality filter.
        """
        where = (f"year BETWEEN {start.year} AND {end.year} "
                 f"AND {COB_DATE} BETWEEN DATE '{start}' AND DATE '{end}' "
                 f"AND {VERSION_ID} = {version} ")
        if assets is not None:
            where += self._in(ASSET_ID, [int(a) for a in assets])
        if factors is not None:
            where += self._in(FACTOR_ID, list(factors))
        if pub_type is not None:
            where += f"AND {TYPE} = '{pub_type}' "
        glob = self.fact_glob(fact, model_id)
        return self.sql(
            f"SELECT * FROM read_parquet('{glob}', hive_partitioning=true) "
            f"WHERE {where}").drop("year", MODEL_ID)

    def has_column(self, fact: str, model_id: str, col: str) -> bool:
        """Schema probe (cached): does this fact table carry the column?"""
        key = (fact, model_id, col)
        if key not in self._cols:
            glob = self.fact_glob(fact, model_id)
            described = self.sql(
                f"DESCRIBE SELECT * FROM read_parquet('{glob}')")
            self._cols[key] = col in described["column_name"].to_list()
        return self._cols[key]

    def date_bounds(self, model_id: str) -> tuple[date, date]:
        """First and last COB date a model has data for.

        Pruned to the first and last year hive partitions: the years come
        from the file listing alone (no parquet footers), so a remote store
        answers from two partition scans instead of a full-glob footer
        sweep — the difference between ~1s and ~50s over the internet.
        """
        glob = self.fact_glob("specific_risk", model_id)
        listing = self.sql(f"SELECT file FROM glob('{glob}')")
        years = (listing["file"].str.extract(r"year=(\d+)", 1)
                 .cast(pl.Int32).drop_nulls())
        if years.is_empty():
            raise ValueError(f"{model_id}: no dated specific_risk "
                             f"files under {glob}")
        row = self.sql(
            f"SELECT min({COB_DATE}) lo, max({COB_DATE}) hi "
            f"FROM read_parquet('{glob}', hive_partitioning=true) "
            f"WHERE year IN ({years.min()}, {years.max()})").to_dicts()[0]
        return row["lo"], row["hi"]

    # ------------------------------------------------------------ dimensions
    def dim(self, name: str) -> pl.DataFrame:
        if name not in _DIMS:
            raise ValueError(f"unknown dimension {name!r}; known: {_DIMS}")
        if name not in self._dims:
            self._dims[name] = self.sql(
                f"SELECT * FROM read_parquet('{self.root}/normalized/{name}.parquet')")
        return self._dims[name]

    # ------------------------------------------------------------------ paths
    def fact_glob(self, fact: str, model_id: str) -> str:
        if fact not in _FACTS:
            raise ValueError(f"unknown fact {fact!r}; known: {_FACTS}")
        return f"{self.root}/normalized/{fact}/{MODEL_ID}={model_id}/**/*.parquet"

    def generic_cs_glob(self, model_id: str) -> str | None:
        """transforms_b date-major fast path, if this store materialized it."""
        base = f"{self.root}/transforms_b/generic_cs/{MODEL_ID}={model_id}"
        if self.root.startswith("s3://") or os.path.isdir(base):
            return f"{base}/**/*.parquet"
        return None


def list_models(root: str | None = None) -> pl.DataFrame:
    """Every model in the store, with vendor, region, size, and conventions."""
    return Store.open(root).dim("model_master")
