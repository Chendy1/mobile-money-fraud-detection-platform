"""Rate limits kept in memory: a token bucket per key and a failure counter per client.

Both are bounded (the oldest entries are dropped), so a caller who invents many names cannot make
the server use unlimited memory. The state is lost when the process restarts, and it is per
process: behind several workers each has its own.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable

MAX_TRACKED = 2000


class RateLimitError(ValueError):
    """A limit that cannot work (zero or negative)."""


class TokenBucket:
    """`per_minute` requests a minute on average, bursts of up to `burst`."""

    def __init__(self, per_minute: float, burst: int, clock: Callable[[], float] = time.monotonic):
        if per_minute <= 0 or burst < 1:
            raise RateLimitError("per_minute must be above zero and burst at least 1")
        self.rate, self.burst, self._clock = per_minute / 60.0, float(burst), clock
        self._state: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, name: str) -> tuple[bool, float]:
        """(allowed, seconds until one request would be allowed)."""
        now = self._clock()
        with self._lock:
            tokens, last = self._state.pop(name, (self.burst, now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            ok = tokens >= 1.0
            if ok:
                tokens -= 1.0
            self._state[name] = (tokens, now)
            while len(self._state) > MAX_TRACKED:
                self._state.popitem(last=False)
            return ok, 0.0 if ok else (1.0 - tokens) / self.rate


class FailureLimiter:
    """Blocks a client after `limit` failures within `window` seconds."""

    def __init__(self, limit: int, window: float, clock: Callable[[], float] = time.monotonic):
        if limit < 1 or window <= 0:
            raise RateLimitError("limit must be at least 1 and window above zero")
        self.limit, self.window, self._clock = limit, window, clock
        self._seen: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    def _recent(self, client: str, now: float) -> deque[float]:
        q = self._seen.get(client)
        if q is None:
            return deque()
        while q and now - q[0] >= self.window:
            q.popleft()
        return q

    def blocked(self, client: str) -> bool:
        with self._lock:
            return len(self._recent(client, self._clock())) >= self.limit

    def retry_after(self, client: str) -> float:
        with self._lock:
            now = self._clock()
            q = self._recent(client, now)
            return max(0.0, self.window - (now - q[0])) if len(q) >= self.limit else 0.0

    def fail(self, client: str) -> None:
        with self._lock:
            now = self._clock()
            q = self._recent(client, now)
            q.append(now)
            self._seen.pop(client, None)
            self._seen[client] = q
            while len(self._seen) > MAX_TRACKED:
                self._seen.popitem(last=False)

    def reset(self, client: str) -> None:
        with self._lock:
            self._seen.pop(client, None)
