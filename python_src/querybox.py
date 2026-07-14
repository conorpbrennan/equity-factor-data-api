"""Client for the factor-query launch-control service on the jump box.

The service keeps AWS credentials off client machines: one authenticated
HTTP call ensures the in-region query box exists, launching it if needed.

    from querybox import ensure, terminate

    box = ensure()                    # blocks (~1 min cold, <1s warm)
    if box:                           # truthiness == alive
        run_my_tests(host=box.public_ip)
        terminate()                   # queries served — shut it down

Configuration comes from the environment (or keyword arguments):
JUMP_SERVICE_URL (e.g. http://18.201.62.69:8422) and JUMP_SERVICE_TOKEN
(the shared secret — arrives by email, goes in your .env, never in code).

Stdlib only — no dependencies to install. CLI mirror of the API:

    python querybox.py ensure|status|terminate
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

_DEFAULT_TIMEOUT = 900.0     # seconds; a cold launch is ~1 min, this is slack


@dataclass(frozen=True)
class QueryBox:
    """One /ensure or /status answer. Truthy iff the box is alive."""
    alive: bool
    public_ip: str | None = None
    private_ip: str | None = None
    state: str | None = None
    detail: str | None = None

    def __bool__(self) -> bool:
        return self.alive


def _call(path: str, *, method: str = "GET", url: str | None,
          token: str | None, timeout: float) -> dict:
    base = url or os.environ.get("JUMP_SERVICE_URL")
    tok = token or os.environ.get("JUMP_SERVICE_TOKEN")
    if not base or not tok:
        raise ValueError("set JUMP_SERVICE_URL and JUMP_SERVICE_TOKEN "
                         "(or pass url=/token=)")
    req = urllib.request.Request(
        base.rstrip("/") + path, method=method,
        headers={"Authorization": f"Bearer {tok}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise PermissionError(
                "jump service rejected the token — check "
                "JUMP_SERVICE_TOKEN") from None
        raise RuntimeError(f"jump service error {e.code}: "
                           f"{e.read().decode(errors='replace')[:200]}") from None


def ensure(*, url: str | None = None, token: str | None = None,
           timeout: float = _DEFAULT_TIMEOUT) -> QueryBox:
    """Ensure the query box is alive; launch it if absent. BLOCKING.

    Returns once the box answers its readiness probe (~1 min cold, <1s if
    already up) — the returned object is truthy iff it is alive, so
    ``if ensure(): ...`` is the whole protocol. Re-polls the service while
    the box is booting; gives back ``alive=False`` (with detail) only if
    the deadline passes first.

    Raises:
        PermissionError: bad token.
        ValueError: no URL/token configured.
        RuntimeError / URLError: service unreachable or 5xx.
    """
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return QueryBox(alive=False, detail=f"not ready in {timeout:.0f}s")
        d = _call("/ensure", url=url, token=token, timeout=left)
        box = QueryBox(alive=d.get("alive", False),
                       public_ip=d.get("public_ip"),
                       private_ip=d.get("private_ip"),
                       detail=d.get("detail"))
        # alive, or failed for a reason retrying won't fix (launch error)
        if box or not box.public_ip:
            return box
        time.sleep(5)                    # up but bootstrap unfinished — poll


def status(*, url: str | None = None, token: str | None = None,
           timeout: float = 30.0) -> QueryBox:
    """Non-launching probe: what state is the query box in right now?"""
    d = _call("/status", url=url, token=token, timeout=timeout)
    return QueryBox(alive=d.get("alive", False), public_ip=d.get("public_ip"),
                    private_ip=d.get("private_ip"), state=d.get("state"))


def terminate(*, url: str | None = None, token: str | None = None,
              timeout: float = 90.0) -> bool:
    """Terminate the query box (idempotent). True if one was terminated."""
    d = _call("/terminate", method="POST", url=url, token=token,
              timeout=timeout)
    return bool(d.get("terminated"))


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "ensure":
        print(ensure())
    elif cmd == "terminate":
        print("terminated:", terminate())
    elif cmd == "status":
        print(status())
    else:
        sys.exit(f"usage: {sys.argv[0]} ensure|status|terminate")
