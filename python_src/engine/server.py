"""Thin in-region DuckDB query engine (the v1 plan's deferred server-side tier).

POST /query {"sql": "..."} -> Arrow IPC stream (zstd-compressed buffers).
GET  /health               -> {"status": "ok", "queries_served": n}

DuckDB runs next to the S3 data with the DuckLake catalog attached; clients
receive result bytes only. Engine warmth is persistent infrastructure: its
range cache outlives client sessions, so a client's "cold" is connect +
in-region compute + result transfer.

Env: DUCKLAKE_CATALOG (local file path), S3 creds via AWS_* (or instance role),
ENGINE_THREADS / ENGINE_MEM.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time

import duckdb
import pyarrow as pa
import pyarrow.ipc
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

app = FastAPI()
_served = 0
_lock = threading.Lock()
_last_activity = time.monotonic()

# Idle shutdown: once queries stop arriving for ENGINE_IDLE_TTL seconds the
# watchdog powers the instance off — clients relaunch through the jump
# service on their next cold session. /health pings do NOT count as
# activity, so monitoring can't keep an idle box alive. 0 disables.
IDLE_TTL = int(os.environ.get("ENGINE_IDLE_TTL", 900))


def _idle_watchdog() -> None:
    while True:
        time.sleep(30)
        if IDLE_TTL and time.monotonic() - _last_activity > IDLE_TTL:
            subprocess.run(["sudo", "shutdown", "-h", "now"])
            return


if IDLE_TTL:
    threading.Thread(target=_idle_watchdog, daemon=True).start()


def _connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(f"SET threads = {os.environ.get('ENGINE_THREADS', 4)}")
    con.execute(f"SET memory_limit = '{os.environ.get('ENGINE_MEM', '20GB')}'")
    con.execute("INSTALL httpfs; LOAD httpfs; INSTALL ducklake;")
    con.execute("SET http_retries = 8; SET http_retry_wait_ms = 1000;")
    if os.environ.get("AWS_ACCESS_KEY_ID"):
        con.execute(f"""CREATE SECRET s3cred (TYPE s3,
            KEY_ID '{os.environ["AWS_ACCESS_KEY_ID"]}',
            SECRET '{os.environ["AWS_SECRET_ACCESS_KEY"]}',
            REGION '{os.environ.get("AWS_DEFAULT_REGION", "eu-west-1")}')""")
    else:
        con.execute("INSTALL aws; LOAD aws;")
        con.execute("CREATE SECRET s3cred (TYPE s3, PROVIDER credential_chain, "
                    "REGION 'eu-west-1')")
    cat = os.environ["DUCKLAKE_CATALOG"]
    con.execute(f"ATTACH 'ducklake:{cat}' AS dl (READ_ONLY)")
    return con


CON = _connect()


class Query(BaseModel):
    sql: str


@app.post("/query")
def query(q: Query) -> Response:
    global _served, _last_activity
    _last_activity = time.monotonic()
    try:
        # concurrent cursors on one database instance: shared buffer pool and
        # range cache, true parallel execution (GIL released inside duckdb)
        table = CON.cursor().execute(q.sql).fetch_arrow_table()
        with _lock:
            _served += 1
        _last_activity = time.monotonic()   # long queries count to their end
    except Exception as e:               # surface engine errors to the client
        raise HTTPException(status_code=400, detail=str(e)[:2000])
    sink = pa.BufferOutputStream()
    opts = pa.ipc.IpcWriteOptions(compression="zstd")
    with pa.ipc.new_stream(sink, table.schema, options=opts) as w:
        w.write_table(table)
    return Response(content=sink.getvalue().to_pybytes(),
                    media_type="application/vnd.apache.arrow.stream")


@app.get("/health")
def health():
    return {"status": "ok", "queries_served": _served,
            "idle_seconds": round(time.monotonic() - _last_activity),
            "idle_ttl": IDLE_TTL}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")
