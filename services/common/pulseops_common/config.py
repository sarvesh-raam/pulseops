from pydantic_settings import BaseSettings, SettingsConfigDict


class BaseServiceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    log_level: str = "INFO"
    # OTLP/HTTP collector base URL, e.g. http://jaeger:4318. Empty = tracing
    # still runs in-process (trace ids in logs) but nothing is exported.
    otel_exporter_otlp_endpoint: str = ""
    # Shared secret for /admin/faults. Empty disables fault injection.
    fault_admin_token: str = ""
    # Kubernetes sets this from the pod name; docker from the container id.
    hostname: str = ""
