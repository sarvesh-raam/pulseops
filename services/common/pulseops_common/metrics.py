"""RED metrics (Rate, Errors, Duration) with identical names and labels in
every service, so one Grafana dashboard and one set of alert rules cover
the whole fleet."""

import time

from prometheus_client import Counter, Gauge, Histogram
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

REQUESTS = Counter(
    "http_server_requests_total",
    "HTTP requests served",
    ["service", "method", "route", "status"],
)
LATENCY = Histogram(
    "http_server_request_duration_seconds",
    "HTTP request latency",
    ["service", "method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
IN_FLIGHT = Gauge("http_server_requests_in_flight", "Requests being served", ["service"])

CLIENT_REQUESTS = Counter(
    "http_client_requests_total",
    "Outbound calls to other services",
    ["service", "target", "outcome"],  # outcome: success | error | timeout | circuit_open
)
CLIENT_RETRIES = Counter("http_client_retries_total", "Outbound call retries", ["service", "target"])
CIRCUIT_STATE = Gauge(
    "circuit_breaker_state",
    "Circuit breaker state: 0 closed, 1 half-open, 2 open",
    ["service", "target"],
)
FAULTS_ACTIVE = Gauge(
    "pulseops_fault_injection_active",
    "1 while an injected fault is active on this instance",
    ["service", "kind"],
)

_SKIP = {"/metrics", "/healthz", "/readyz"}


class MetricsMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, service: str):
        super().__init__(app)
        self.service = service

    async def dispatch(self, request: Request, call_next):
        if request.url.path in _SKIP:
            return await call_next(request)
        IN_FLIGHT.labels(self.service).inc()
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            IN_FLIGHT.labels(self.service).dec()
            # route template, never the raw path: ids in paths would
            # explode label cardinality
            route = getattr(request.scope.get("route"), "path", "unmatched")
            REQUESTS.labels(self.service, request.method, route, str(status)).inc()
            LATENCY.labels(self.service, request.method, route).observe(time.perf_counter() - start)
