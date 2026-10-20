"""Putting late-arriving events back in order, the way a stream processor does with a watermark.

The scoring service needs each customer's transactions in time order. Events can arrive out of
order, so they wait here until no event older than them can still arrive: the watermark is the
newest event time seen minus `max_lateness`. Whatever is older than the watermark is released in
(event time, id) order. An event that arrives after something newer was already released is too
late to place and is dropped (a real system would send it to a dead-letter queue). An id seen
before is dropped as a duplicate.
"""

from __future__ import annotations

import heapq
from collections import OrderedDict

from mmrisk.streaming.events import Event


class ReorderBuffer:
    def __init__(self, max_lateness_seconds: float, remember: int = 10_000):
        if max_lateness_seconds < 0 or remember < 1:
            raise ValueError("max_lateness_seconds must be >= 0 and remember >= 1")
        self.max_lateness = max_lateness_seconds
        self._heap: list[tuple] = []
        self._waiting: set[str] = set()
        self._released: OrderedDict[str, None] = OrderedDict()
        self._remember = remember
        self._newest = None
        self._last: tuple | None = None
        self.max_waiting = 0

    def __len__(self) -> int:
        return len(self._heap)

    def push(self, event: Event) -> tuple[list[Event], list[tuple[Event, str]]]:
        """Returns (events now released in order, [(event, reason)] dropped); reason is
        'duplicate' or 'late'."""
        tid, t = event.transaction_id, event.event_time
        if tid in self._waiting or tid in self._released:
            return [], [(event, "duplicate")]
        if self._last is not None and (t, tid) < self._last:
            return [], [(event, "late")]
        heapq.heappush(self._heap, (t, tid, event.seq, event))
        self._waiting.add(tid)
        self.max_waiting = max(self.max_waiting, len(self._heap))
        if self._newest is None or t > self._newest:
            self._newest = t
        released = []
        while self._heap and (self._newest - self._heap[0][0]).total_seconds() > self.max_lateness:
            released.append(self._pop())
        return released, []

    def flush(self) -> list[Event]:
        """Release everything still waiting (the stream has ended)."""
        out = []
        while self._heap:
            out.append(self._pop())
        return out

    def _pop(self) -> Event:
        t, tid, _, event = heapq.heappop(self._heap)
        self._waiting.discard(tid)
        self._last = (t, tid)
        self._released[tid] = None
        if len(self._released) > self._remember:
            self._released.popitem(last=False)
        return event
