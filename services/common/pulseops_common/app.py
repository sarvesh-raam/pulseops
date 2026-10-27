import logging
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Info, generate_latest

from pulseops_common.config import BaseServiceSettings
from pulseops_common.faults import FaultMiddleware, FaultState, fault_router
from pulseops_common.logging import configure_logging
from pulseops_common.metrics import MetricsMiddleware
from pulseops_common.tracing import configure_tracing, instrument_app, shutdown_tracing

ReadinessCheck = Callable[[], Awaitable[None]]
Lifecycle = Callable[[FastAPI], AsyncIterator[None]]

BUILD = Info("pulseops_service", "Service identity")


def create_service_app(
    service: str,
    *,
    settings: BaseServiceSettings | None = None,
    lifespan: Lifecycle | None = None,
    readiness_check: ReadinessCheck | None = None,
    version: str = "1.0.0",
) -> FastAPI:
    settings = settings or BaseServiceSettings()
    instance = settings.hostname or socket.gethostname()
    configure_logging(service, settings.log_level)
    configure_tracing(service, settings.otel_exporter_otlp_endpoint, instance)
    BUILD.info({"service": service, "version": version, "instance": instance})
    logger = logging.getLogger(f"pulseops.{service}")

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        logger.info("starting", extra={"fields": {"instance": instance}})
        if lifespan is not None:
            async with asynccontextmanager(lifespan)(app):
                yield
        else:
            yield
        logger.info("stopped")
        shutdown_tracing()

    app = FastAPI(title=f"pulseops-{service}", version=version, lifespan=_lifespan)
    faults = FaultState(service)
    app.state.faults = faults

    # order matters: metrics wraps faults, so injected latency/errors show
    # up in the RED metrics exactly like real ones would
    app.add_middleware(FaultMiddleware, state=faults)
    app.add_middleware(MetricsMiddleware, service=service)
    app.include_router(fault_router(faults, settings.fault_admin_token))
    instrument_app(app)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error", extra={"fields": {"path": request.url.path}})
        return JSONResponse(status_code=500, content={"error": "internal error"})

    # Liveness: the process can serve HTTP. Never checks dependencies — a
    # database outage must not make Kubernetes restart every pod.
    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"status": "ok", "service": service, "instance": instance}

    # Readiness: can this instance do useful work right now? A failing
    # check takes the pod out of its Service until it recovers.
    @app.get("/readyz", include_in_schema=False)
    async def readyz(response: Response) -> dict:
        if readiness_check is not None:
            try:
                await readiness_check()
            except Exception as exc:
                response.status_code = 503
                return {"status": "unavailable", "service": service, "reason": type(exc).__name__}
        return {"status": "ready", "service": service, "instance": instance}

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app
