"""PulseOps control plane: the API behind the console.

Aggregates what the console shows — topology, replica health, golden
signals from Prometheus, alerts from Alertmanager — and runs chaos
experiments against the runtime (Kubernetes or Docker).
"""

import asyncio
import hmac
import logging
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from prometheus_client import Gauge
from pydantic import BaseModel, Field

from controlplane.chaos import ChaosEngine, ChaosError
from controlplane.prom import Prometheus
from controlplane.runtime import Pod, detect_runtime
from pulseops_common import BaseServiceSettings, create_service_app

logger = logging.getLogger("pulseops.controlplane")

REPLICAS = Gauge("pulseops_service_replicas", "Replicas per service", ["service", "state"])
ALERTS_FIRING = Gauge("pulseops_alerts_firing", "Alerts currently firing (from Alertmanager)")

DEFAULT_TOPOLOGY = {
    "loadgen": ["gateway"],
    "gateway": ["orders", "inventory"],
    "orders": ["inventory", "payments", "postgres"],
    "inventory": ["postgres"],
    "payments": ["postgres"],
    "postgres": [],
}


class Settings(BaseServiceSettings):
    runtime_mode: str = "auto"  # auto | kubernetes | docker
    compose_project: str = "pulseops-platform"
    target_namespace: str = ""  # kubernetes: namespace of the workloads (default: our own)
    prometheus_url: str = "http://prometheus:9090"
    chaos_enabled: bool = True
    chaos_targets: str = "gateway,orders,inventory,payments,postgres,loadgen"
    # optional: require X-PulseOps-Token on mutating chaos calls
    control_plane_token: str = ""
    recovery_timeout_sec: float = 180.0
    # links the console renders (browser-facing URLs)
    grafana_url: str = "http://localhost:3000"
    prometheus_public_url: str = "http://localhost:9090"
    jaeger_url: str = "http://localhost:16686"
    alertmanager_url: str = "http://localhost:9093"


class KillRequest(BaseModel):
    pod: str | None = None
    service: str | None = None


class FaultRequest(BaseModel):
    service: str
    latency_ms: int = Field(default=0, ge=0, le=10_000)
    error_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    duration_sec: int = Field(default=60, ge=5, le=600)


settings = Settings()
targets = {t.strip() for t in settings.chaos_targets.split(",") if t.strip()}
ctx: dict = {"runtime": None, "chaos": None, "prom": None, "alerts": {}}


async def _refresh_gauges_forever() -> None:
    while True:
        try:
            if ctx["runtime"] is not None:
                desired = await ctx["runtime"].desired_replicas()
                pods = await ctx["runtime"].list_pods()
                for service, count in desired.items():
                    REPLICAS.labels(service, "desired").set(count)
                    REPLICAS.labels(service, "ready").set(sum(1 for p in pods if p.service == service and p.ready))
        except Exception:
            logger.warning("replica refresh failed", exc_info=True)
        await asyncio.sleep(5)


async def lifespan(app: FastAPI):
    runtime = detect_runtime(settings.runtime_mode, settings.compose_project, settings.target_namespace)
    if runtime is None:
        logger.warning("no runtime detected (no in-cluster credentials, no docker socket): read-only mode")
    else:
        logger.info("runtime detected", extra={"fields": {"runtime": runtime.name}})
        ctx["chaos"] = ChaosEngine(
            runtime, targets, fault_token=settings.fault_admin_token,
            recovery_timeout_sec=settings.recovery_timeout_sec,
        )
    ctx["runtime"] = runtime
    ctx["prom"] = Prometheus(settings.prometheus_url)
    task = asyncio.create_task(_refresh_gauges_forever())
    yield
    task.cancel()
    if ctx["chaos"]:
        await ctx["chaos"].aclose()
    if runtime:
        await runtime.aclose()
    await ctx["prom"].aclose()


app = create_service_app("controlplane", settings=settings, lifespan=lifespan)


def require_chaos(x_pulseops_token: str | None = Header(default=None)) -> ChaosEngine:
    if not settings.chaos_enabled:
        raise HTTPException(status_code=403, detail="chaos experiments are disabled (CHAOS_ENABLED=false)")
    if settings.control_plane_token and not (
        x_pulseops_token and hmac.compare_digest(x_pulseops_token, settings.control_plane_token)
    ):
        raise HTTPException(status_code=401, detail="missing or invalid X-PulseOps-Token")
    if ctx["chaos"] is None:
        raise HTTPException(status_code=503, detail="no runtime available to run experiments against")
    return ctx["chaos"]


def _pod_dict(p: Pod) -> dict:
    age = (datetime.now(timezone.utc) - p.started_at).total_seconds() if p.started_at else None
    return {
        "id": p.id, "name": p.name, "service": p.service, "status": p.status, "ready": p.ready,
        "restarts": p.restarts, "started_at": p.started_at.isoformat() if p.started_at else None,
        "age_sec": round(age) if age is not None else None, "ip": p.ip, "node": p.node,
        "killable": p.service in targets and p.ready,
    }


def _health(desired: int, ready: int, error_rate: float | None) -> str:
    if desired == 0 or ready == 0:
        return "down"
    if ready < desired or (error_rate or 0) > 0.05:
        return "degraded"
    return "healthy"


@app.get("/api/overview")
async def overview() -> dict:
    pods: list[Pod] = []
    desired: dict[str, int] = {}
    runtime_error = None
    if ctx["runtime"] is not None:
        try:
            pods, desired = await asyncio.gather(ctx["runtime"].list_pods(), ctx["runtime"].desired_replicas())
        except Exception as exc:
            runtime_error = f"{type(exc).__name__}: {exc}"
    signals: dict = {}
    user_success = None
    prom_error = None
    try:
        signals, user_success = await asyncio.gather(ctx["prom"].golden_signals(), ctx["prom"].user_success_rate())
    except Exception as exc:
        prom_error = f"{type(exc).__name__}"

    names = list(DEFAULT_TOPOLOGY) + sorted(set(desired) - set(DEFAULT_TOPOLOGY))
    services = []
    for name in names:
        mine = [p for p in pods if p.service == name]
        if name not in desired and not mine:
            continue
        sig = signals.get(name, {})
        ready = sum(1 for p in mine if p.ready)
        want = desired.get(name, len(mine))
        services.append({
            "name": name,
            "depends_on": DEFAULT_TOPOLOGY.get(name, []),
            "desired": want,
            "ready": ready,
            "health": _health(want, ready, sig.get("error_rate")),
            "rps": sig.get("rps"),
            "error_rate": sig.get("error_rate"),
            "p95_ms": sig.get("p95_ms"),
            "chaos_target": name in targets,
            "pods": [_pod_dict(p) for p in sorted(mine, key=lambda p: p.name)],
        })

    firing = [a for a in ctx["alerts"].values() if a["status"] == "firing"]
    return {
        "runtime": ctx["runtime"].name if ctx["runtime"] else None,
        "runtime_error": runtime_error,
        "prometheus_error": prom_error,
        "chaos_enabled": settings.chaos_enabled and ctx["chaos"] is not None,
        "token_required": bool(settings.control_plane_token),
        "user_success_rate": user_success,
        "services": services,
        "alerts_firing": len(firing),
        "links": {
            "grafana": settings.grafana_url,
            "prometheus": settings.prometheus_public_url,
            "jaeger": settings.jaeger_url,
            "alertmanager": settings.alertmanager_url,
        },
    }


@app.get("/api/timeseries")
async def timeseries(minutes: int = Query(15, ge=1, le=360)) -> dict:
    step = max(5, minutes * 60 // 120)
    try:
        return await ctx["prom"].timeseries(minutes, step)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"prometheus unavailable: {type(exc).__name__}") from None


@app.get("/api/experiments")
async def experiments() -> list[dict]:
    return [e.to_dict() for e in ctx["chaos"].experiments] if ctx["chaos"] else []


@app.post("/api/chaos/kill")
async def chaos_kill(body: KillRequest, chaos: ChaosEngine = Depends(require_chaos)) -> dict:
    try:
        experiment = await chaos.kill(pod_id=body.pod, service=body.service)
    except ChaosError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return experiment.to_dict()


@app.post("/api/chaos/faults")
async def chaos_faults(body: FaultRequest, chaos: ChaosEngine = Depends(require_chaos)) -> dict:
    if not body.latency_ms and not body.error_rate:
        raise HTTPException(status_code=422, detail="set latency_ms and/or error_rate")
    try:
        experiment = await chaos.inject(
            body.service, latency_ms=body.latency_ms, error_rate=body.error_rate, duration_sec=body.duration_sec
        )
    except ChaosError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return experiment.to_dict()


@app.delete("/api/chaos/faults/{service}")
async def chaos_clear(service: str, chaos: ChaosEngine = Depends(require_chaos)) -> dict:
    try:
        return {"service": service, "cleared_replicas": await chaos.clear_faults(service)}
    except ChaosError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


# --- alerts ----------------------------------------------------------------


@app.post("/api/alerts/webhook", status_code=204)
async def alertmanager_webhook(payload: dict) -> None:
    """Alertmanager webhook receiver: alerts are *delivered* here and shown
    in the console (plug any other receiver — Slack, PagerDuty, email —
    into deploy/alertmanager/alertmanager.yml alongside it)."""
    for alert in payload.get("alerts", []):
        fingerprint = alert.get("fingerprint") or str(sorted(alert.get("labels", {}).items()))
        ctx["alerts"][fingerprint] = {
            "fingerprint": fingerprint,
            "status": alert.get("status", "firing"),
            "name": alert.get("labels", {}).get("alertname", "unknown"),
            "severity": alert.get("labels", {}).get("severity", "none"),
            "service": alert.get("labels", {}).get("service"),
            "summary": alert.get("annotations", {}).get("summary", ""),
            "starts_at": alert.get("startsAt"),
            "ends_at": alert.get("endsAt") if alert.get("status") == "resolved" else None,
            "received_at": datetime.now(timezone.utc).isoformat(),
        }
    if len(ctx["alerts"]) > 200:  # keep the most recent
        for key in sorted(ctx["alerts"], key=lambda k: ctx["alerts"][k]["received_at"])[:-200]:
            del ctx["alerts"][key]
    ALERTS_FIRING.set(sum(1 for a in ctx["alerts"].values() if a["status"] == "firing"))


@app.get("/api/alerts")
async def list_alerts() -> list[dict]:
    newest_first = sorted(ctx["alerts"].values(), key=lambda a: a["received_at"], reverse=True)
    return sorted(newest_first, key=lambda a: a["status"] != "firing")  # stable: firing on top
