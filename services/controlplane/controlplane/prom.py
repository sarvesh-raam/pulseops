"""Thin Prometheus HTTP API client for the console's golden signals."""

import math
import time

import httpx

WINDOW = "1m"

QUERIES = {
    "rps": f"sum by (service) (rate(http_server_requests_total[{WINDOW}]))",
    # `or ... * 0`: a service with no 5xx series yet reports 0%, not "no data"
    "error_rate": (
        f'(sum by (service) (rate(http_server_requests_total{{status=~"5.."}}[{WINDOW}]))'
        f" or sum by (service) (rate(http_server_requests_total[{WINDOW}])) * 0)"
        f" / sum by (service) (rate(http_server_requests_total[{WINDOW}]))"
    ),
    "p95_ms": (
        f"1000 * histogram_quantile(0.95, sum by (service, le) "
        f"(rate(http_server_request_duration_seconds_bucket[{WINDOW}])))"
    ),
}
# the customer's view: share of synthetic user requests that didn't 5xx
USER_SUCCESS = (
    f'sum(rate(loadgen_requests_total{{outcome="ok"}}[{WINDOW}])) / sum(rate(loadgen_requests_total[{WINDOW}]))'
)


def _num(value: str) -> float | None:
    f = float(value)
    return None if math.isnan(f) or math.isinf(f) else f


class Prometheus:
    def __init__(self, base_url: str):
        self.client = httpx.AsyncClient(base_url=base_url, timeout=5)

    async def aclose(self) -> None:
        await self.client.aclose()

    async def query(self, expr: str) -> list[dict]:
        r = await self.client.get("/api/v1/query", params={"query": expr})
        r.raise_for_status()
        return r.json()["data"]["result"]

    async def query_range(self, expr: str, minutes: int, step: int) -> list[dict]:
        end = time.time()
        r = await self.client.get(
            "/api/v1/query_range",
            params={"query": expr, "start": end - minutes * 60, "end": end, "step": step},
        )
        r.raise_for_status()
        return r.json()["data"]["result"]

    async def golden_signals(self) -> dict[str, dict[str, float | None]]:
        out: dict[str, dict[str, float | None]] = {}
        for metric, expr in QUERIES.items():
            for series in await self.query(expr):
                service = series["metric"].get("service", "unknown")
                out.setdefault(service, {})[metric] = _num(series["value"][1])
        return out

    async def user_success_rate(self) -> float | None:
        result = await self.query(USER_SUCCESS)
        return _num(result[0]["value"][1]) if result else None

    async def timeseries(self, minutes: int = 15, step: int = 15) -> dict:
        out: dict = {"services": {}, "user_success": []}
        for metric, expr in QUERIES.items():
            for series in await self.query_range(expr, minutes, step):
                service = series["metric"].get("service", "unknown")
                out["services"].setdefault(service, {})[metric] = [
                    [int(t), _num(v)] for t, v in series["values"]
                ]
        for series in await self.query_range(USER_SUCCESS, minutes, step):
            out["user_success"] = [[int(t), _num(v)] for t, v in series["values"]]
        return out
