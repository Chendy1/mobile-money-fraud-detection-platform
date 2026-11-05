"""Sending the planned requests: one thread per worker, each with its own keep-alive connection.

`run_worker` is plain code over an injected clock, sleep and sender, so its timing logic is tested
without threads or a network. `HttpSender` uses http.client (standard library).
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from collections.abc import Callable
from urllib.parse import urlparse

from mmrisk.loadtest.plan import Request
from mmrisk.loadtest.stats import Sample

LEAD_SECONDS = 0.3  # workers start this long after the run is set up, so none starts late
GIVE_UP_AFTER = 20  # consecutive connection failures in one worker: the server is not there
MAX_BODY = 1_000_000


def wire(txn: dict) -> dict:
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in txn.items()}


class HttpSender:
    """Posts one transaction to /v1/score over this thread's own connection."""

    def __init__(self, url: str, key: str | None = None, timeout: float = 10.0):
        parts = urlparse(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("url must start with http:// or https:// and name a host")
        self.scheme, self.host, self.port = parts.scheme, parts.hostname, parts.port
        self.timeout = timeout
        self._headers = {"content-type": "application/json"}
        if key:
            self._headers["authorization"] = f"Bearer {key}"
        self._local = threading.local()

    def _connection(self) -> http.client.HTTPConnection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            cls = (
                http.client.HTTPSConnection
                if self.scheme == "https"
                else http.client.HTTPConnection
            )
            conn = self._local.conn = cls(self.host, self.port, timeout=self.timeout)
        return conn

    def __call__(self, txn: dict) -> tuple[int, bool]:
        """(status, duplicate); status 0 when there was no response."""
        body = json.dumps(wire(txn)).encode()
        for attempt in (1, 2):  # a keep-alive connection the server closed gets one fresh try
            conn = self._connection()
            try:
                conn.request("POST", "/v1/score", body=body, headers=self._headers)
                resp = conn.getresponse()
                data = resp.read(MAX_BODY)
                duplicate = False
                if resp.status == 200:
                    try:
                        duplicate = bool(json.loads(data).get("duplicate"))
                    except ValueError:
                        pass
                return resp.status, duplicate
            except (OSError, http.client.HTTPException) as exc:
                conn.close()
                self._local.conn = None
                if isinstance(exc, (socket.timeout, TimeoutError)) or attempt == 2:
                    return 0, False
        return 0, False  # pragma: no cover

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None


def run_worker(
    items: list[Request],
    base: float,
    sender: Callable[[dict], tuple[int, bool]],
    clock: Callable[[], float] = time.perf_counter,
    sleep: Callable[[float], None] = time.sleep,
    stop: threading.Event | None = None,
) -> list[Sample]:
    """Send `items` (in order) each at base + offset; sample times are seconds after `base`."""
    out: list[Sample] = []
    failures = 0
    for item in items:
        if stop is not None and stop.is_set():
            break
        wait = base + item.offset - clock()
        if wait > 0:
            sleep(wait)
        started = clock()
        status, duplicate = sender(item.txn)
        ended = clock()
        out.append(
            Sample(
                item.index,
                item.segment,
                item.offset,
                started - base,
                ended - base,
                status,
                duplicate,
            )
        )
        failures = failures + 1 if status == 0 else 0
        if failures >= GIVE_UP_AFTER:
            if stop is not None:
                stop.set()
            break
    return out


def run(requests: list[Request], sender, workers: int) -> tuple[list[Sample], int]:
    """Run the whole plan. Returns the samples and how many requests were never sent."""
    lanes: list[list[Request]] = [[] for _ in range(workers)]
    for r in requests:
        lanes[r.worker].append(r)
    stop = threading.Event()
    results: list[list[Sample]] = [[] for _ in range(workers)]
    base = time.perf_counter() + LEAD_SECONDS

    def work(i: int) -> None:
        try:
            results[i] = run_worker(lanes[i], base, sender, stop=stop)
        finally:
            close = getattr(sender, "close", None)
            if close:
                close()

    threads = [threading.Thread(target=work, args=(i,), daemon=True) for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    samples = sorted((s for r in results for s in r), key=lambda s: s.index)
    return samples, len(requests) - len(samples)
