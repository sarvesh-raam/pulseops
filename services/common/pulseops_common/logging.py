import json
import logging
import sys
import time

from opentelemetry import trace


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with the active trace/span id so Grafana
    can jump from a Loki log line straight to the trace in Jaeger."""

    # timestamps end in "Z", so they must actually be UTC
    converter = time.gmtime

    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S") + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "service": self.service,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        span = trace.get_current_span().get_span_context()
        if span.is_valid:
            payload["trace_id"] = format(span.trace_id, "032x")
            payload["span_id"] = format(span.span_id, "016x")
        extra = getattr(record, "fields", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(service: str, level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # uvicorn's access log duplicates our metrics/traces; keep its errors only
    logging.getLogger("uvicorn.access").disabled = True
    # httpx logs every outbound request at INFO — pure noise next to traces
    logging.getLogger("httpx").setLevel(logging.WARNING)
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True


def log_fields(**fields) -> dict:
    """Structured fields for a log call: logger.info("msg", extra=log_fields(order_id=...))."""
    return {"fields": fields}
