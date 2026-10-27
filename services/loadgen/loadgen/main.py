"""Synthetic traffic: a steady stream of shoppers browsing and ordering
through the gateway, so every dashboard has live data and every chaos
experiment has a measurable user-facing impact (the success rate seen
here is the "customer view" of an outage)."""

import asyncio
import logging
import random
import signal
import time

import httpx
from prometheus_client import Counter, Histogram, start_http_server
from pydantic_settings import BaseSettings, SettingsConfigDict

from pulseops_common.logging import configure_logging

logger = logging.getLogger("pulseops.loadgen")

REQUESTS = Counter("loadgen_requests_total", "Synthetic user requests", ["action", "outcome"])
LATENCY = Histogram(
    "loadgen_request_duration_seconds", "User-perceived latency", ["action"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
)

SKUS = ["KB-01", "MS-02", "MN-03", "HS-04", "DK-05"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")
    gateway_url: str = "http://gateway:8000"
    requests_per_sec: float = 5.0
    order_ratio: float = 0.4
    metrics_port: int = 9100
    log_level: str = "INFO"


async def _one(client: httpx.AsyncClient, settings: Settings) -> None:
    if random.random() < settings.order_ratio:
        action = "order"
        items = [{"sku": random.choice(SKUS), "qty": random.randint(1, 3)} for _ in range(random.randint(1, 3))]
        call = client.post("/api/orders", json={"items": items})
    else:
        action = "browse"
        call = client.get("/api/products")

    start = time.perf_counter()
    try:
        response = await call
        # 2xx and business rejections (402/409) mean the system worked;
        # 5xx and transport errors are what a user experiences as an outage
        outcome = "ok" if response.status_code < 500 else "error"
    except httpx.HTTPError:
        outcome = "error"
    LATENCY.labels(action).observe(time.perf_counter() - start)
    REQUESTS.labels(action, outcome).inc()


async def main() -> None:
    settings = Settings()
    configure_logging("loadgen", settings.log_level)
    start_http_server(settings.metrics_port)
    logger.info("generating load", extra={"fields": {"rps": settings.requests_per_sec, "target": settings.gateway_url}})

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass

    in_flight: set[asyncio.Task] = set()
    interval = 1.0 / max(settings.requests_per_sec, 0.1)
    async with httpx.AsyncClient(base_url=settings.gateway_url, timeout=10) as client:
        while not stop.is_set():
            # open-loop arrivals: fire on schedule regardless of how slow the
            # system is, like real users — a closed loop would hide outages
            if len(in_flight) < 200:
                task = asyncio.create_task(_one(client, settings))
                in_flight.add(task)
                task.add_done_callback(in_flight.discard)
            try:
                await asyncio.wait_for(stop.wait(), timeout=random.expovariate(1 / interval))
            except TimeoutError:
                pass
        if in_flight:
            await asyncio.wait(in_flight, timeout=10)


if __name__ == "__main__":
    asyncio.run(main())
