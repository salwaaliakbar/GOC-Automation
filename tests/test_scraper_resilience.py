"""Offline tests for retry, circuit breaker and cancellation in the register client."""

import threading
import time

import pytest
import requests

from goc_collector.scraper import RateLimiter, RegisterClient, RegisterError


class TimeoutSession:
    headers = {}

    def __init__(self):
        self.calls = 0

    def request(self, *a, **kw):
        self.calls += 1
        raise requests.Timeout("simulated timeout")


def test_retries_then_raises_register_error():
    c = RegisterClient(min_interval=0.2, max_retries=3, backoff_base=0.01)
    c.limiter.breaker_cooldown = 0.01
    sess = TimeoutSession()
    c._local.session = sess
    c._session = lambda: sess
    with pytest.raises(RegisterError):
        c._request("GET", "https://example.invalid/")
    assert sess.calls == 3


def test_circuit_breaker_pauses_after_consecutive_failures():
    rl = RateLimiter(0.2, breaker_failures=3, breaker_cooldown=0.5)
    rl.record_failure()
    assert rl.min_interval == pytest.approx(0.2)   # one isolated failure: no slowdown
    for _ in range(2):
        rl.record_failure()
    assert rl.cooldowns == 1
    t = time.monotonic()
    rl.wait()
    assert time.monotonic() - t >= 0.4
    # success resets the failure streak and the interval relaxes again
    for _ in range(100):  # relaxes back to base after successes
        rl.record_success()
    assert rl.min_interval == pytest.approx(0.2)


def test_cancel_aborts_backoff_quickly():
    c = RegisterClient(min_interval=0.2, max_retries=4, backoff_base=30)
    sess = TimeoutSession()
    c._session = lambda: sess
    err = {}

    def go():
        try:
            c._request("GET", "https://example.invalid/")
        except RegisterError as e:
            err["e"] = str(e)

    th = threading.Thread(target=go)
    th.start()
    time.sleep(0.3)
    t = time.monotonic()
    c.cancel()
    th.join(5)
    assert not th.is_alive() and time.monotonic() - t < 2
    assert "cancelled" in err["e"]


def test_trickling_response_is_cut_off_by_total_deadline():
    class Trickle:
        status_code = 200
        headers = {}

        def iter_content(self, n):
            while True:
                time.sleep(0.2)
                yield b"x"

        def close(self):
            pass

    class Sess:
        headers = {}

        def request(self, *a, **kw):
            return Trickle()

    c = RegisterClient(min_interval=0.2, max_retries=1, total_timeout=1.0)
    sess = Sess()
    c._session = lambda: sess
    t = time.monotonic()
    with pytest.raises(RegisterError):
        c._request("GET", "https://example.invalid/")
    assert time.monotonic() - t < 3


def test_one_broken_page_does_not_trip_circuit_breaker():
    """Retries of the same page count once, so one bad page never pauses everything."""
    c = RegisterClient(min_interval=0.2, max_retries=3, backoff_base=0.01)
    c.limiter.breaker_failures = 2
    sess = TimeoutSession()
    c._session = lambda: sess
    with pytest.raises(RegisterError):
        c._request("GET", "https://example.invalid/")
    assert sess.calls == 3
    assert c.limiter.cooldowns == 0
    assert c.limiter.min_interval == pytest.approx(0.2)


def test_min_gap_after_slow_answer():
    """The next request waits min_gap after the previous answer, even after a slow one."""
    rl = RateLimiter(0.2, min_gap=0.5)
    rl.wait()
    time.sleep(0.4)              # a "slow" answer, longer than the interval
    rl.request_done(0.4)
    t = time.monotonic()
    rl.wait()
    assert time.monotonic() - t >= 0.45


def test_slow_median_widens_interval_but_single_slow_page_does_not():
    rl = RateLimiter(1.0, slow_median=5.0, min_gap=0)
    for e in (0.5, 0.6, 20.0, 0.7, 0.5, 0.8):    # one heavy page among fast ones
        assert rl.request_done(e) is False
    assert rl.min_interval == pytest.approx(1.0)
    for e in (9, 9, 9, 9, 9, 9):                  # register slow overall
        rl.request_done(e)
    assert rl.min_interval > 1.0


def test_waiting_seconds_ignores_normal_spacing():
    c = RegisterClient(min_interval=10.0)
    c.limiter.wait()                              # next slot is 10 s away
    assert c.limiter.pause_remaining() > 5
    assert c.waiting_seconds() == 0               # not shown as "the website is busy"
    c.limiter.penalise(30)
    assert c.waiting_seconds() > 25
