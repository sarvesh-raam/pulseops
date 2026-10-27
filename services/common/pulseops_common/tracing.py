import logging

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger("pulseops.tracing")

_configured = False


def configure_tracing(service: str, otlp_endpoint: str, instance: str) -> None:
    """Install a tracer provider once per process. Spans always exist (so
    logs get trace ids and context propagates across services through the
    W3C traceparent header); they're only exported when an endpoint is set."""
    global _configured
    if _configured:
        return
    provider = TracerProvider(
        resource=Resource.create({"service.name": service, "service.instance.id": instance})
    )
    if otlp_endpoint:
        exporter = OTLPSpanExporter(endpoint=f"{otlp_endpoint.rstrip('/')}/v1/traces", timeout=5)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        logger.info("exporting traces", extra={"fields": {"endpoint": otlp_endpoint}})
    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument()
    _configured = True


def instrument_app(app) -> None:
    FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz,metrics")


def shutdown_tracing() -> None:
    provider = trace.get_tracer_provider()
    if hasattr(provider, "shutdown"):
        provider.shutdown()  # flush buffered spans before the process exits
