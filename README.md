# PulseOps

An observability and chaos-engineering platform for microservices. It runs a
small shop (gateway, orders, inventory, payments) under constant synthetic
traffic, watches it with metrics, logs, traces and alerts, and lets you break
it on purpose — kill a pod, inject latency or errors — then measures how fast
the system heals.

- **Metrics:** Prometheus scrapes RED metrics (rate, errors, duration) from
  every service, with the same names and labels everywhere, so one Grafana
  dashboard and one set of alert rules cover the whole fleet.
- **Logs:** JSON logs, collected by Promtail into Loki. Every line carries
  the trace id, so Grafana can jump from a log line to its trace.
- **Traces:** OpenTelemetry across gateway → orders → inventory/payments,
  exported to Jaeger.
- **Alerts:** 8 Prometheus rules (service down, error rate, latency, circuit
  breaker open, customer impact, failed chaos recovery, …), routed through
  Alertmanager and shown in the console.
- **Self-healing:** Kubernetes probes, PodDisruptionBudgets and HPAs heal
  pods, and the services heal requests (timeouts, retries with jittered
  backoff, circuit breakers, an idempotent saga that resumes after a crash).
- **Chaos console:** one button kills a live pod; the control plane then
  watches until every replica is Ready again and records the recovery time.
- **CI/CD:** GitHub Actions lints, tests, validates every manifest, and
  proves self-healing on a real kind cluster on every push, then publishes
  the images to GHCR.

## Quick start (Docker Compose)

```bash
docker compose up -d --build
```

| What | URL |
|---|---|
| Console (topology, chaos, alerts) | http://localhost:8088 |
| Grafana (dashboards, Loki logs) | http://localhost:3001 (admin / admin) |
| Prometheus | http://localhost:9095 |
| Alertmanager | http://localhost:9093 |
| Jaeger (traces) | http://localhost:16686 |
| Shop API through the console | http://localhost:8088/shop/api/products |

Ports can be changed with `CONSOLE_PORT`, `GRAFANA_PORT`, `PROMETHEUS_PORT`,
`ALERTMANAGER_PORT` and `JAEGER_PORT`.

## Kubernetes (kind)

```bash
kind create cluster --config environments/kind/kind-cluster.yaml
docker compose build
kind load docker-image --name pulseops \
  pulseops/gateway:local pulseops/orders:local pulseops/inventory:local \
  pulseops/payments:local pulseops/loadgen:local pulseops/controlplane:local pulseops/console:local
kubectl apply -k environments/kind
kubectl get pods -A -w
```

The kind cluster maps the UIs to localhost: console on
http://localhost:18088, Grafana on :13001, Prometheus on :19095,
Alertmanager on :19093, Jaeger on :26686.

For any other cluster, `kubectl apply -k deploy` uses the images CD
publishes to `ghcr.io/<owner>/pulseops-*`. Before using it for anything
real, replace the demo values in `deploy/k8s/workloads/config.yaml` and
`deploy/k8s/system/controlplane.yaml` (Postgres password, fault token), and
set `CONTROL_PLANE_TOKEN` to protect the chaos endpoints.

## Chaos experiments

From the console, or the control plane API:

```bash
# kill a random ready pod of a service
curl -X POST localhost:8088/api/chaos/kill -H 'content-type: application/json' -d '{"service":"orders"}'

# add 800ms latency and 20% errors to every payments replica for 60s
curl -X POST localhost:8088/api/chaos/faults -H 'content-type: application/json' \
  -d '{"service":"payments","latency_ms":800,"error_rate":0.2,"duration_sec":60}'

# experiment history, with measured recovery times
curl localhost:8088/api/experiments
```

What heals what:

| Failure | Detected by | Recovery |
|---|---|---|
| Pod killed (Kubernetes) | Deployment / StatefulSet controller | replacement pod; traffic only after its readiness probe passes |
| Container killed (Compose) | Docker restart policy | same container restarts |
| Wedged process | liveness probe `/healthz` | container restarted |
| Lost database | readiness probe `/readyz` | pod leaves the Service until Postgres is back (no restart storm) |
| Request hits a dying pod | `ResilientClient` | retry with jittered backoff lands on a healthy replica |
| Dependency down | circuit breaker | fail fast instead of piling up timeouts, probe again after a cool-down |
| Orders pod killed mid-saga | saga lease expires | another replica's recovery loop resumes the order |
| Payment outcome unknown (timeout) | orders saga | charge is voided and stock released, so nobody pays for a failed order |

The control plane only has the Kubernetes permissions it needs: list pods
and replica counts, and delete pods, in the workload namespace only
(`deploy/k8s/system/controlplane.yaml`).

## Layout

```
services/               Python (FastAPI) services, one Dockerfile for all
  common/               shared library: logging, tracing, RED metrics, probes,
                        fault injection, retries + circuit breaker
  gateway/ orders/ inventory/ payments/   the shop
  loadgen/              synthetic customers (open-loop traffic)
  controlplane/         console API: topology, golden signals, chaos engine
  tests/
console/                React + Vite console, served by unprivileged nginx
deploy/                 Kustomize base (kubectl apply -k deploy)
  k8s/                  workloads, control plane, observability stack
  observability/        Prometheus, Alertmanager, Loki, Promtail, Grafana
                        configs, shared by Compose and Kubernetes
environments/kind/      local cluster definition + overlay
scripts/                Grafana dashboard generator
.github/workflows/      CI (lint, tests, config validation, kind e2e) and CD
```

## Development

```bash
cd services
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install ./common pytest pytest-asyncio ruff
docker run -d --rm -p 5434:5432 -e POSTGRES_USER=pulseops -e POSTGRES_PASSWORD=pulseops -e POSTGRES_DB=pulseops postgres:16-alpine
pytest -q && ruff check .
```

Tests use a real Postgres (set `TEST_DATABASE_URL` to point elsewhere).
The console: `cd console && npm ci && npm run dev`.

## CI/CD

- **CI** (`.github/workflows/ci.yml`, every push and PR): lint and tests
  against Postgres; console typecheck and build; Kustomize renders validated
  with kubeconform; Prometheus, alert and Alertmanager configs checked with
  promtool and amtool; then an end-to-end job that creates a kind cluster,
  deploys everything, sends real orders, kills an `orders` pod and the
  `postgres` pod through the chaos API, and fails unless both recover.
- **CD** (`.github/workflows/cd.yml`, after CI passes on `main`): builds and
  pushes `ghcr.io/<owner>/pulseops-<service>` tagged with the commit SHA and
  `latest`. With the repo variable `DEPLOY_ENABLED=true` and the secret
  `KUBE_CONFIG` (base64 kubeconfig), it also pins those images, applies the
  manifests, waits for every rollout, smoke-tests the gateway, and rolls
  back on failure.

## License

MIT
