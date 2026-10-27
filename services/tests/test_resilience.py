import httpx
import pytest

from pulseops_common.resilience import CircuitBreaker, CircuitOpenError, ResilientClient, UpstreamError


def _client(handler, **kw) -> ResilientClient:
    return ResilientClient("test", f"t{id(handler)}", "http://upstream", transport=httpx.MockTransport(handler), **kw)


async def test_retries_transient_failures_then_succeeds():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503) if len(calls) < 3 else httpx.Response(200, json={"ok": True})

    client = _client(handler, max_attempts=3)
    response = await client.request("GET", "/x")
    assert response.status_code == 200 and len(calls) == 3


async def test_client_errors_are_returned_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(409, json={"detail": "nope"})

    response = await _client(handler).request("POST", "/x")
    assert response.status_code == 409 and len(calls) == 1


async def test_connection_errors_exhaust_into_upstream_error():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(UpstreamError):
        await _client(handler, max_attempts=2).request("GET", "/x")


async def test_circuit_opens_and_fails_fast():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503)

    breaker = CircuitBreaker("test", "flaky", failure_threshold=3, reset_after_sec=60)
    client = ResilientClient("test", "flaky", "http://u", transport=httpx.MockTransport(handler), breaker=breaker, max_attempts=3)
    with pytest.raises(UpstreamError):
        await client.request("GET", "/x")
    assert breaker.state == CircuitBreaker.OPEN
    before = len(calls)
    with pytest.raises(CircuitOpenError):
        await client.request("GET", "/x")
    assert len(calls) == before  # no call made while open


def test_circuit_half_opens_after_cooldown_and_closes_on_success(monkeypatch):
    breaker = CircuitBreaker("test", "b", failure_threshold=1, reset_after_sec=5)
    breaker.record_failure()
    assert not breaker.allow()
    import pulseops_common.resilience as r

    real = r.time.monotonic
    monkeypatch.setattr(r.time, "monotonic", lambda: real() + 10)
    assert breaker.allow() and breaker.state == CircuitBreaker.HALF_OPEN
    breaker.record_success()
    assert breaker.state == CircuitBreaker.CLOSED


def test_half_open_failure_reopens_immediately(monkeypatch):
    breaker = CircuitBreaker("test", "c", failure_threshold=5, reset_after_sec=5)
    for _ in range(5):
        breaker.record_failure()
    import pulseops_common.resilience as r

    real = r.time.monotonic
    monkeypatch.setattr(r.time, "monotonic", lambda: real() + 10)
    assert breaker.allow()
    breaker.record_failure()
    assert breaker.state == CircuitBreaker.OPEN
