---
title: Pulse
emoji: ⚡
colorFrom: indigo
colorTo: purple
sdk: docker
app_port: 7860
pinned: false
---

# Pulse — Distributed Job Scheduler

A production-inspired distributed background-job platform: atomic job
claiming across any number of workers, configurable retries with backoff and
jitter, a dead letter queue with one-click replay, workflow dependencies,
cron/recurring jobs, live dashboard updates, and AI-generated failure
summaries — all on a single Postgres instance, no Redis required.

## Quick start

```bash
cp .env.example .env       # defaults work out of the box; add GROQ_API_KEY to see AI summaries
docker compose up -d --build
```

That's it — one command brings up Postgres, runs migrations, seeds demo
data, and starts the API, scheduler, worker, frontend, Prometheus and Grafana.

- **Dashboard:** http://localhost:5173 — log in with `demo@pulse.dev` / `demo1234`
- **API:** http://localhost:8000 (interactive docs at `/docs`, Prometheus metrics at `/metrics`)
- **Prometheus:** http://localhost:9090 (targets, 7 alert rules)
- **Grafana:** http://localhost:3000 — the *Pulse — Overview* dashboard is the home page (anonymous view, `admin`/`admin` to edit)
- **Postgres:** `localhost:5432` (user/pass `pulse`/`pulse`, db `pulse`)

Scale workers to see distributed claiming in action (Prometheus discovers
the new replicas automatically):

```bash
docker compose up -d --scale worker=3
```

### Chaos demo: kill a worker and watch it heal

1. Dashboard → **Load Generator** → *Blast Jobs* (or use a long `sleep` job).
2. Dashboard or Workers → **Kill a random worker** (or the skull on one
   worker's card; or `python backend/cli.py cluster kill --project <id>`).
3. Watch: the worker process exits with code 137 mid-job → Docker (or
   Kubernetes) starts a replacement within seconds → after
   `VISIBILITY_TIMEOUT_SEC` (30s) the reaper marks the old worker dead and
   requeues its stranded jobs, each with a `WorkerLost` attempt in its
   execution history → the new worker finishes them. Grafana's
   *Self-healing* panel shows the kill and the recoveries.

On Kubernetes the button deletes the worker's **pod** (`gracePeriodSeconds=0`)
instead; see [Kubernetes](#kubernetes).

## What's here

| Area | Where |
|---|---|
| System architecture, process responsibilities, job lifecycle | [docs/architecture.md](docs/architecture.md) |
| Database schema (13 tables), indexes, cascades | [docs/er-diagram.md](docs/er-diagram.md) |
| REST API reference | [docs/api.md](docs/api.md) |
| Design trade-offs (Postgres vs. Redis, normalization, at-least-once vs. exactly-once, retries/jitter, idempotency) + all 8 bonus features | [docs/design-decisions.md](docs/design-decisions.md) |

## Kubernetes

`k8s/base` is a Kustomize tree: namespace (Pod Security `restricted`),
config + secret, demo Postgres StatefulSet, a migration Job, and Deployments
for the API (2 replicas, HPA, PDB), scheduler (2 replicas, leader-elected),
workers (3 replicas, HPA, PDB, liveness probe on the heartbeat) and
frontend, plus an Ingress. Every container runs non-root with a read-only
root filesystem.

```bash
kubectl apply -k k8s/base                 # everything
kubectl apply -k k8s/monitoring           # optional: ServiceMonitors + PrometheusRule (Prometheus Operator)
kubectl -n pulse get pods -w              # then hit the chaos button and watch a worker pod get replaced
```

Before using it for anything real: replace the placeholder `pulse-secrets`
Secret (real `JWT_SECRET`, managed-database `DATABASE_URL`), drop
`postgres.yaml`, and set the Ingress host and `CORS_ORIGINS`.

## CI/CD

- **CI** (`.github/workflows/ci.yml`, every push and PR): backend lint
  (ruff) + migrations up/down/up + the full pytest suite against a real
  Postgres service container; frontend lint + typecheck + build; Kustomize
  render validated with kubeconform and the Prometheus config/rules with
  promtool; Docker builds on PRs.
- **CD** (`.github/workflows/cd.yml`, after CI passes on `main`): builds and
  pushes `ghcr.io/<owner>/pulseops-backend` and `pulseops-frontend` tagged with the
  commit SHA and `latest`. If the repo variable `DEPLOY_ENABLED=true` and the
  secret `KUBE_CONFIG` (base64 kubeconfig) are set, it also pins those
  images in Kustomize, runs the migration Job, waits for every rollout,
  smoke-tests `/readyz`, and rolls back automatically on failure.

## Tech stack

- **Backend:** FastAPI, SQLAlchemy 2.0 (async), Pydantic v2, Alembic, Uvicorn
- **Database:** PostgreSQL 16 — `FOR UPDATE SKIP LOCKED` for atomic claiming, `LISTEN/NOTIFY` for event-driven wake-up
- **Worker/Scheduler:** standalone async Python processes, same codebase as the API, different entrypoints
- **Auth:** JWT (access + refresh), bcrypt via passlib, RBAC (owner/admin/member/viewer)
- **Realtime:** FastAPI WebSockets with polling fallback
- **AI failure summaries:** Groq (`llama-3.3-70b-versatile`)
- **Frontend:** React + Vite + TypeScript, Tailwind v4, shadcn/ui, TanStack Query, Zustand, Recharts, react-router
- **Testing:** pytest, pytest-asyncio, httpx
- **Ops:** Prometheus (`prometheus-client`), Grafana, Kubernetes (Kustomize), GitHub Actions, GHCR

## Project layout

```
pulse/
├── docker-compose.yml      postgres, migrate, seed, api, scheduler, worker, frontend, prometheus, grafana
├── .env.example
├── .github/workflows/      ci.yml (lint, tests, manifest validation), cd.yml (GHCR publish + k8s rollout)
├── k8s/                    base/ (Kustomize app + RBAC + probes/HPA/PDB), monitoring/ (Prometheus Operator)
├── deploy/                 prometheus/ (scrape config + alerts), grafana/ (provisioned dashboard), huggingface/
├── docs/                   architecture.md, er-diagram.md, api.md, design-decisions.md
├── backend/
│   ├── alembic/            migrations (hand-written, not autogenerated — see design-decisions.md)
│   ├── app/
│   │   ├── main.py         FastAPI app factory, routers, WS, lifespan
│   │   ├── config.py       pydantic-settings
│   │   ├── models/         one file per table
│   │   ├── schemas/        Pydantic request/response models
│   │   ├── api/            routers: auth, orgs, projects, retry_policies, queues, jobs, workers, dlq, metrics
│   │   ├── core/           security, rbac, errors, pagination, ratelimit, notify, logging
│   │   ├── engine/         claim.py, retry.py, scheduler.py, reaper.py, dispatcher.py, ai_summary.py
│   │   ├── worker/         worker entrypoint, runtime (concurrency + graceful shutdown), demo handlers
│   │   ├── ws/             WebSocket manager + event publisher
│   │   └── seed.py         one-shot demo data seed
│   └── tests/
└── frontend/
    └── src/
        ├── pages/           Landing, Login, Register, Onboarding, Dashboard, Queues(+detail), Jobs(+detail), Workers, Dlq, Settings
        ├── components/      layout/, charts/, shared/, ui/ (shadcn)
        └── lib/             api client, auth store, WebSocket hook
```

## Running tests

The test suite needs a real Postgres (SKIP LOCKED and LISTEN/NOTIFY aren't
meaningfully testable against SQLite) — it creates and migrates its own
`pulse_test` database on the same Postgres server docker-compose already
brings up, so no extra setup beyond having the stack running:

```bash
cd backend
python -m venv .venv && .venv/Scripts/activate   # or source .venv/bin/activate on macOS/Linux
pip install -e ".[dev]"
pytest -v
ruff check app tests cli.py
```

Postgres somewhere other than `localhost:5432`? Set `TEST_PG_HOST` /
`TEST_PG_PORT`. The suite only ever touches its own `pulse_test` database.

Covers the critical paths per the rubric: claim atomicity under concurrent
claimants (including a concurrency-limit-under-contention variant), retry
backoff math for all three strategies, DLQ transition exactly-once, reaper
recovery (including the "worker marked dead by its own graceful shutdown"
edge case), idempotent enqueue (including the concurrent-race variant), auth
+ RBAC (401/403), and a full register → org → project → queue → job →
claim → poll-to-completed smoke test.

## Environment variables

See [`.env.example`](.env.example) for the full list with defaults. Nothing
is required beyond copying it as-is; `GROQ_API_KEY` is optional (AI failure
summaries silently no-op without it — see design-decisions.md §6).

## Demo data

The `seed` service (a one-shot container, exits after running) creates:
`demo@pulse.dev` / `demo1234`, an org/project, an exponential retry policy,
three queues (`emails`, `reports`, `webhooks`) with different concurrency/
priority settings, and a mix of immediate/delayed/batch/cron/deliberately-
failing jobs so the dashboard is populated the moment you log in. It's
idempotent — safe to re-run (`docker compose up seed`) without duplicating data.
