"""Shared building blocks for every PulseOps microservice.

    from pulseops_common import create_service_app

    app = create_service_app("orders", readiness_check=db_ping)

gives a FastAPI app that already has: JSON logs carrying trace ids,
OpenTelemetry tracing (exported over OTLP when OTEL_EXPORTER_OTLP_ENDPOINT
is set), RED metrics at /metrics, /healthz + /readyz probes, and the
/admin/faults chaos hooks.
"""

from pulseops_common.app import create_service_app
from pulseops_common.config import BaseServiceSettings

__all__ = ["BaseServiceSettings", "create_service_app"]
