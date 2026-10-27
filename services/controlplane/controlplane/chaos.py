"""Chaos experiments and recovery measurement.

A pod-kill experiment is only interesting if you can see the system heal,
so every kill is followed by a watcher that polls the runtime until the
service is whole again — every desired replica Ready, the victim gone or
restarted — and records the time that took (MTTR). Faults (latency /
errors) are fanned out to every replica of the target service and expire
on their own.
"""

import asyncio
import logging
import random
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime

import httpx
from prometheus_client import Counter, Histogram

from controlplane.runtime import Pod, Runtime, now

logger = logging.getLogger("pulseops.chaos")

EXPERIMENTS = Counter("pulseops_chaos_experiments_total", "Chaos experiments started", ["kind", "service"])
RECOVERY = Histogram(
    "pulseops_chaos_recovery_seconds", "Time from pod kill until the service was fully ready again",
    ["service"], buckets=(1, 2, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180),
)
FAILED_RECOVERIES = Counter("pulseops_chaos_recovery_failures_total", "Kills the service did not recover from in time", ["service"])


class ChaosError(Exception):
    pass


@dataclass
class Experiment:
    id: str
    kind: str  # pod-kill | latency | errors
    service: str
    target: str
    started_at: datetime
    status: str = "running"  # running | recovered | failed | active | expired
    detail: str = ""
    ended_at: datetime | None = None
    recovery_seconds: float | None = None
    params: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["started_at"] = self.started_at.isoformat()
        data["ended_at"] = self.ended_at.isoformat() if self.ended_at else None
        return data


class ChaosEngine:
    def __init__(
        self,
        runtime: Runtime,
        targets: set[str],
        *,
        fault_token: str,
        service_port: int = 8000,
        recovery_timeout_sec: float = 180.0,
        poll_interval_sec: float = 1.0,
        history: int = 100,
    ):
        self.runtime = runtime
        self.targets = targets
        self.fault_token = fault_token
        self.service_port = service_port
        self.recovery_timeout_sec = recovery_timeout_sec
        self.poll_interval_sec = poll_interval_sec
        self.experiments: deque[Experiment] = deque(maxlen=history)
        self._tasks: set[asyncio.Task] = set()
        self._http = httpx.AsyncClient(timeout=3)

    async def aclose(self) -> None:
        for task in self._tasks:
            task.cancel()
        await self._http.aclose()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _check_target(self, service: str) -> None:
        if service not in self.targets:
            raise ChaosError(f"'{service}' is not a chaos target (allowed: {', '.join(sorted(self.targets))})")

    # --- pod kill -------------------------------------------------------

    async def kill(self, *, pod_id: str | None = None, service: str | None = None) -> Experiment:
        pods = [p for p in await self.runtime.list_pods() if p.service in self.targets]
        if pod_id:
            victim = next((p for p in pods if p.id == pod_id or p.name == pod_id), None)
            if victim is None:
                raise ChaosError(f"no pod '{pod_id}' among chaos targets")
        else:
            candidates = [p for p in pods if p.ready and (service is None or p.service == service)]
            if service:
                self._check_target(service)
            if not candidates:
                raise ChaosError("no ready pod to kill" + (f" for '{service}'" if service else ""))
            victim = random.choice(candidates)

        desired = (await self.runtime.desired_replicas()).get(victim.service, 1)
        await self.runtime.kill(victim)
        experiment = Experiment(
            id=uuid.uuid4().hex[:12], kind="pod-kill", service=victim.service, target=victim.name,
            started_at=now(), detail=f"killed {victim.name} via {self.runtime.name}",
            params={"desired_replicas": desired, "restarts_before": victim.restarts},
        )
        self.experiments.appendleft(experiment)
        EXPERIMENTS.labels("pod-kill", victim.service).inc()
        logger.warning("pod killed", extra={"fields": {"pod": victim.name, "service": victim.service, "runtime": self.runtime.name}})
        self._spawn(self._watch_recovery(experiment, victim, desired))
        return experiment

    def _is_recovered(self, victim: Pod, pods: list[Pod], desired: int, started: datetime) -> bool:
        mine = [p for p in pods if p.service == victim.service]
        ready = [p for p in mine if p.ready]
        if len(ready) < desired:
            return False
        if self.runtime.name == "docker":
            # same container is restarted in place: it must have come back
            # up *after* the kill
            again = next((p for p in mine if p.id == victim.id), None)
            return again is not None and again.ready and again.started_at is not None and again.started_at > started
        # kubernetes: the victim must be gone (a replacement pod took its place)
        return all(p.name != victim.name for p in ready)

    async def _watch_recovery(self, experiment: Experiment, victim: Pod, desired: int) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.recovery_timeout_sec
        # give the runtime a moment to notice the kill, so a poll in the
        # first few ms can't see the old pod still "ready" and declare a
        # zero-second recovery
        await asyncio.sleep(min(self.poll_interval_sec, 1.0))
        while loop.time() < deadline:
            try:
                pods = await self.runtime.list_pods()
                if self._is_recovered(victim, pods, desired, experiment.started_at):
                    experiment.ended_at = now()
                    experiment.recovery_seconds = round((experiment.ended_at - experiment.started_at).total_seconds(), 2)
                    experiment.status = "recovered"
                    experiment.detail += f"; recovered in {experiment.recovery_seconds}s"
                    RECOVERY.labels(victim.service).observe(experiment.recovery_seconds)
                    logger.info("service recovered", extra={"fields": {"service": victim.service, "seconds": experiment.recovery_seconds}})
                    return
            except Exception:
                logger.exception("recovery poll failed")
            await asyncio.sleep(self.poll_interval_sec)
        experiment.status = "failed"
        experiment.ended_at = now()
        experiment.detail += f"; NOT recovered within {self.recovery_timeout_sec:.0f}s"
        FAILED_RECOVERIES.labels(victim.service).inc()

    # --- fault injection ---------------------------------------------------

    async def inject(self, service: str, *, latency_ms: int, error_rate: float, duration_sec: int) -> Experiment:
        self._check_target(service)
        if not self.fault_token:
            raise ChaosError("fault injection is disabled (FAULT_ADMIN_TOKEN not set)")
        pods = [p for p in await self.runtime.list_pods() if p.service == service and p.ready and p.ip]
        if not pods:
            raise ChaosError(f"no ready pods for '{service}'")
        spec = {"latency_ms": latency_ms, "error_rate": error_rate, "duration_sec": duration_sec}
        results = await asyncio.gather(*(self._post_fault(p, spec) for p in pods), return_exceptions=True)
        ok = sum(1 for r in results if r is True)
        if ok == 0:
            raise ChaosError(f"no replica of '{service}' accepted the fault ({results[0]!r})")

        kind = "latency" if latency_ms and not error_rate else "errors" if error_rate and not latency_ms else "latency+errors"
        parts = [f"+{latency_ms}ms" if latency_ms else "", f"{error_rate:.0%} errors" if error_rate else ""]
        experiment = Experiment(
            id=uuid.uuid4().hex[:12], kind=kind, service=service, target=f"{ok}/{len(pods)} replicas",
            started_at=now(), status="active", params=spec,
            detail=f"{' and '.join(p for p in parts if p)} for {duration_sec}s",
        )
        self.experiments.appendleft(experiment)
        EXPERIMENTS.labels(kind, service).inc()
        self._spawn(self._expire(experiment, duration_sec))
        return experiment

    async def _post_fault(self, pod: Pod, spec: dict) -> bool:
        r = await self._http.post(
            f"http://{pod.ip}:{self.service_port}/admin/faults", json=spec,
            headers={"X-Admin-Token": self.fault_token},
        )
        r.raise_for_status()
        return True

    async def _expire(self, experiment: Experiment, duration_sec: int) -> None:
        await asyncio.sleep(duration_sec)
        if experiment.status == "active":
            experiment.status = "expired"
            experiment.ended_at = now()

    async def clear_faults(self, service: str) -> int:
        self._check_target(service)
        pods = [p for p in await self.runtime.list_pods() if p.service == service and p.ip]
        cleared = 0
        for pod in pods:
            try:
                r = await self._http.delete(
                    f"http://{pod.ip}:{self.service_port}/admin/faults", headers={"X-Admin-Token": self.fault_token}
                )
                cleared += r.is_success
            except httpx.HTTPError:
                pass
        for exp in self.experiments:
            if exp.service == service and exp.status == "active":
                exp.status = "expired"
                exp.ended_at = now()
                exp.detail += "; cleared manually"
        return cleared
