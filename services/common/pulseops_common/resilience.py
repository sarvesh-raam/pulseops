"""Application-level self-healing for service-to-service calls.

Kubernetes heals *pods*; this heals *requests* while a pod is being
replaced: short timeouts, retries with exponential backoff + full jitter
for transient failures, and a circuit breaker that stops hammering a
dependency that is clearly down (failing fast instead of piling up
timeouts), then probes it again after a cool-down.
"""

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable

import httpx

from pulseops_common.metrics import CIRCUIT_STATE, CLIENT_REQUESTS, CLIENT_RETRIES

logger = logging.getLogger("pulseops.resilience")

RETRYABLE_STATUS = {502, 503, 504}


class CircuitOpenError(Exception):
    def __init__(self, target: str):
        super().__init__(f"circuit open for {target}")
        self.target = target


class UpstreamError(Exception):
    def __init__(self, target: str, status: int | None, detail: str):
        super().__init__(f"{target} failed ({status}): {detail}")
        self.target = target
        self.status = status


class CircuitBreaker:
    CLOSED, HALF_OPEN, OPEN = 0, 1, 2

    def __init__(self, service: str, target: str, failure_threshold: int = 5, reset_after_sec: float = 10.0):
        self.service = service
        self.target = target
        self.failure_threshold = failure_threshold
        self.reset_after_sec = reset_after_sec
        self.failures = 0
        self.state = self.CLOSED
        self.opened_at = 0.0
        self._publish()

    def _publish(self) -> None:
        CIRCUIT_STATE.labels(self.service, self.target).set(self.state)

    def allow(self) -> bool:
        if self.state == self.OPEN:
            if time.monotonic() - self.opened_at >= self.reset_after_sec:
                self.state = self.HALF_OPEN  # let one trial request through
                self._publish()
                return True
            return False
        return True

    def record_success(self) -> None:
        if self.state != self.CLOSED:
            logger.info("circuit closed", extra={"fields": {"target": self.target}})
        self.failures = 0
        self.state = self.CLOSED
        self._publish()

    def record_failure(self) -> None:
        self.failures += 1
        if self.state == self.HALF_OPEN or self.failures >= self.failure_threshold:
            if self.state != self.OPEN:
                logger.warning("circuit opened", extra={"fields": {"target": self.target, "failures": self.failures}})
            self.state = self.OPEN
            self.opened_at = time.monotonic()
        self._publish()


def backoff_delay(attempt: int, base: float = 0.05, cap: float = 1.0) -> float:
    """Full-jitter exponential backoff (attempt is 1-based)."""
    return random.uniform(0, min(cap, base * 2 ** (attempt - 1)))


class ResilientClient:
    """httpx wrapper used for every call to another service."""

    def __init__(
        self,
        service: str,
        target: str,
        base_url: str,
        *,
        timeout: float = 2.0,
        max_attempts: int = 3,
        breaker: CircuitBreaker | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.service = service
        self.target = target
        self.max_attempts = max_attempts
        self.breaker = breaker or CircuitBreaker(service, target)
        self.client = httpx.AsyncClient(base_url=base_url, timeout=timeout, transport=transport)

    async def aclose(self) -> None:
        await self.client.aclose()

    async def request(self, method: str, path: str, *, retry: bool = True, **kwargs) -> httpx.Response:
        """Return the response for any non-retryable status (2xx and 4xx).
        Raise UpstreamError when attempts are exhausted, CircuitOpenError
        when the breaker is open. Only idempotent calls should pass retry=True."""
        attempts = self.max_attempts if retry else 1
        last_error = "unknown"
        last_status: int | None = None
        for attempt in range(1, attempts + 1):
            if not self.breaker.allow():
                CLIENT_REQUESTS.labels(self.service, self.target, "circuit_open").inc()
                raise CircuitOpenError(self.target)
            try:
                response = await self.client.request(method, path, **kwargs)
            except httpx.TimeoutException:
                outcome, last_error, last_status = "timeout", "timeout", None
            except httpx.TransportError as exc:
                # connection refused/reset: typically the pod we were routed
                # to is being killed and the Service hasn't dropped it yet
                outcome, last_error, last_status = "error", type(exc).__name__, None
            else:
                if response.status_code not in RETRYABLE_STATUS:
                    self.breaker.record_success()
                    CLIENT_REQUESTS.labels(self.service, self.target, "success" if response.is_success else "client_error").inc()
                    return response
                outcome, last_error, last_status = "error", response.text[:200], response.status_code

            self.breaker.record_failure()
            CLIENT_REQUESTS.labels(self.service, self.target, outcome).inc()
            if attempt < attempts:
                CLIENT_RETRIES.labels(self.service, self.target).inc()
                await asyncio.sleep(backoff_delay(attempt))
        raise UpstreamError(self.target, last_status, last_error)


async def retry_async[T](fn: Callable[[], Awaitable[T]], attempts: int = 5, base: float = 0.2, cap: float = 5.0) -> T:
    """Retry an arbitrary coroutine (e.g. connecting to Postgres at boot)."""
    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except Exception:
            if attempt == attempts:
                raise
            await asyncio.sleep(backoff_delay(attempt, base, cap) + base)
    raise RuntimeError("unreachable")
