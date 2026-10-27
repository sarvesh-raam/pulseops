# syntax=docker/dockerfile:1
# One recipe for every Python service:
#   docker build --build-arg SERVICE=orders -t pulseops-orders services/
FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY common /src/common
RUN pip install /src/common

FROM python:3.12-slim
ARG SERVICE
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    SERVICE=${SERVICE}
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY --chown=app:app ${SERVICE}/${SERVICE} ./${SERVICE}
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/readyz || exit 1
# --timeout-graceful-shutdown: drain in-flight requests on SIGTERM
CMD ["sh", "-c", "exec uvicorn ${SERVICE}.main:app --host 0.0.0.0 --port 8000 --timeout-graceful-shutdown 10 --no-server-header"]
