"""Unit tests for the sliding-window rate limiter (fake clock, no sleeping)."""

from app.utils.rate_limit import SlidingWindowRateLimiter


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_allows_up_to_max_then_blocks():
    limiter = SlidingWindowRateLimiter(2, 60.0, time_fn=_FakeClock())

    assert limiter.allow("1.2.3.4") is True
    assert limiter.allow("1.2.3.4") is True
    assert limiter.allow("1.2.3.4") is False


def test_window_expiry_restores_allowance():
    clock = _FakeClock()
    limiter = SlidingWindowRateLimiter(1, 60.0, time_fn=clock)

    assert limiter.allow("1.2.3.4") is True
    assert limiter.allow("1.2.3.4") is False

    clock.now = 60.0  # first request has aged out of the window
    assert limiter.allow("1.2.3.4") is True


def test_keys_are_isolated():
    limiter = SlidingWindowRateLimiter(1, 60.0, time_fn=_FakeClock())

    assert limiter.allow("1.2.3.4") is True
    assert limiter.allow("5.6.7.8") is True
    assert limiter.allow("1.2.3.4") is False


def test_idle_buckets_are_pruned():
    clock = _FakeClock()
    limiter = SlidingWindowRateLimiter(5, 60.0, time_fn=clock)

    limiter.allow("old-client")
    clock.now = 120.0
    limiter.allow("new-client")

    assert "old-client" not in limiter._buckets
