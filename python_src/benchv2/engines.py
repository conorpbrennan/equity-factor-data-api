"""Engine-fleet client: round-robin with pre-query liveness check.

Protocol (project decision 2026-07-10): before executing a query, ping the
engine's /health; if the ping fails, re-read the live-engine list (from
ENGINE_LIST_FILE if set, else the initial ENGINE_URL set), health-probe all
candidates, rebuild the rotation from responders, and dispatch to the next
alive engine. A query POST that fails mid-flight triggers the same refresh
and one retry on another engine.
"""

from __future__ import annotations

import itertools
import os
from pathlib import Path

import requests

PING_TIMEOUT = 2.0


class EngineBalancer:
    def __init__(self, urls: list[str] | None = None,
                 list_file: str | None = None):
        self.list_file = list_file or os.environ.get("ENGINE_LIST_FILE")
        self._configured = urls or self._read_config()
        self.sessions: dict[str, requests.Session] = {}
        self._alive_urls = list(self._configured)
        self._rr = itertools.cycle(self._alive_urls)

    def _read_config(self) -> list[str]:
        if self.list_file and Path(self.list_file).exists():
            raw = Path(self.list_file).read_text().strip()
            return [f"http://{h}:8080" if "://" not in h else h
                    for h in raw.split(",") if h]
        return [u.rstrip("/") for u in os.environ["ENGINE_URL"].split(",")]

    def _session(self, url: str) -> requests.Session:
        if url not in self.sessions:
            self.sessions[url] = requests.Session()
        return self.sessions[url]

    def _ping(self, url: str) -> bool:
        try:
            return self._session(url).get(f"{url}/health",
                                          timeout=PING_TIMEOUT).ok
        except requests.RequestException:
            return False

    def refresh(self) -> None:
        """Re-read membership and rebuild the rotation from live engines."""
        self._configured = self._read_config()
        self._alive_urls = [u for u in self._configured if self._ping(u)]
        if not self._alive_urls:
            raise RuntimeError(f"no live engines among {self._configured}")
        self._rr = itertools.cycle(self._alive_urls)

    def next_alive(self) -> str:
        url = next(self._rr)
        if self._ping(url):
            return url
        self.refresh()
        return next(self._rr)

    def query(self, sql: str, timeout: float = 900.0) -> bytes:
        url = self.next_alive()
        try:
            r = self._session(url).post(f"{url}/query", json={"sql": sql},
                                        timeout=timeout)
            r.raise_for_status()
            return r.content
        except requests.RequestException:
            self.refresh()                      # engine died mid-query
            url = self.next_alive()             # one retry elsewhere
            r = self._session(url).post(f"{url}/query", json={"sql": sql},
                                        timeout=timeout)
            r.raise_for_status()
            return r.content
