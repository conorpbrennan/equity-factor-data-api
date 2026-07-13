"""Read-only store handle: connection, dimension tables, per-model paths.

Layout is the genv2 store (generator-spec-v2.md): dimension parquet at the
root, hive-partitioned facts underneath, optional transforms_b fast path.
Local paths and s3:// both work (env AWS creds, region eu-west-1 default).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import duckdb
import polars as pl

from conventions import MODEL_ID

_DIMS = ("model_master", "factor_master", "asset_master", "asset_xref")

_FACTS = ("factor_loading", "factor_covariance", "specific_risk",
          "universe_membership", "factor_return", "fmp")


@dataclass
class Store:
    root: str
    threads: int = int(os.environ.get("DUCK_THREADS", 4))
    memory_limit: str = os.environ.get("DUCK_MEM", "4GB")
    _con: duckdb.DuckDBPyConnection | None = field(default=None, repr=False)
    _dims: dict[str, pl.DataFrame] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.root = self.root.rstrip("/")

    @classmethod
    def open(cls, root: str | None = None) -> "Store":
        root = root or os.environ.get("FACTOR_STORE_ROOT")
        if not root:
            raise ValueError("no store: pass root= or set FACTOR_STORE_ROOT")
        return cls(root=root)

    # ------------------------------------------------------------ connection
    @property
    def con(self) -> duckdb.DuckDBPyConnection:
        if self._con is None:
            con = duckdb.connect()
            con.execute(f"SET threads = {self.threads}")
            con.execute(f"SET memory_limit = '{self.memory_limit}'")
            if self.root.startswith("s3://"):
                con.execute("INSTALL httpfs; LOAD httpfs;")
                con.execute("SET http_retries = 8;")
                if "AWS_ACCESS_KEY_ID" in os.environ:
                    con.execute(f"""CREATE SECRET (TYPE s3,
                        KEY_ID '{os.environ["AWS_ACCESS_KEY_ID"]}',
                        SECRET '{os.environ["AWS_SECRET_ACCESS_KEY"]}',
                        REGION '{os.environ.get("AWS_REGION", "eu-west-1")}')""")
            self._con = con
        return self._con

    def sql(self, query: str) -> pl.DataFrame:
        return pl.from_arrow(self.con.execute(query).arrow())

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
