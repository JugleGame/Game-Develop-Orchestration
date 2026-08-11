"""In-process sliding-window rate limiter for LLM-spend-triggering endpoints.

Deliberately dependency-free for the MVP. State lives in this process only:
with multiple replicas each replica enforces its own window, so move the
bookkeeping to a shared store (e.g. Redis) before scaling out.
"""

import time
from collections import deque
from collections.abc import Callable


class SlidingWindowRateLimiter:
    """Allows at most ``max_requests`` per ``key`` within a rolling window.

    ``time_fn`` is injectable so tests can advance a fake clock instead of
    sleeping.
    """

    def __init__(
        self,
        max_requests: int,
        window_seconds: float = 60.0,
        *,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._time_fn = time_fn
        self._buckets: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        """Record a request for ``key`` and report whether it fits the window."""

        now = self._time_fn()
        bucket = self._buckets.setdefault(key, deque())
        while bucket and now - bucket[0] >= self._window_seconds:
            bucket.popleft()
        if len(bucket) >= self._max_requests:
            return False
        bucket.append(now)
        # Drop buckets idle clients left behind so the per-key map stays bounded.
        idle_keys = [
            k
            for k, b in self._buckets.items()
            if k != key and (not b or now - b[-1] >= self._window_seconds)
        ]
        for idle_key in idle_keys:
            del self._buckets[idle_key]
        return True
