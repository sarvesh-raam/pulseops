import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from controlplane.chaos import ChaosEngine, ChaosError
from controlplane.runtime import Pod


class FakeRuntime:
    """Simulates a Deployment controller: a killed pod disappears and a
    replacement becomes Ready after `heal_after` seconds (never, if None)."""

    name = "kubernetes"

    def __init__(self, heal_after: float | None = 0.3):
        self.heal_after = heal_after
        self.pods = [self._pod("orders", i) for i in range(2)] + [self._pod("payments", 0)]
        self.killed: list[str] = []

    @staticmethod
    def _pod(service, i, ready=True):
        return Pod(id=f"{service}-{i}", name=f"{service}-{i}", service=service, status="Running", ready=ready,
                   restarts=0, started_at=datetime.now(timezone.utc) - timedelta(minutes=5), ip=f"10.0.0.{i}")

    async def list_pods(self):
        return list(self.pods)

    async def desired_replicas(self):
        return {"orders": 2, "payments": 1}

    async def kill(self, pod):
        self.killed.append(pod.name)
        self.pods = [p for p in self.pods if p.name != pod.name]
        if self.heal_after is not None:
            async def heal():
                await asyncio.sleep(self.heal_after)
                self.pods.append(self._pod(pod.service, f"new{len(self.killed)}"))
            asyncio.get_running_loop().create_task(heal())

    async def aclose(self):
        pass


def _engine(runtime, **kw):
    return ChaosEngine(runtime, {"orders", "payments"}, fault_token="tok", poll_interval_sec=0.05, **kw)


async def test_kill_measures_time_to_recovery():
    rt = FakeRuntime(heal_after=0.3)
    engine = _engine(rt)
    exp = await engine.kill(service="orders")
    assert exp.status == "running" and rt.killed
    for _ in range(100):
        if exp.status != "running":
            break
        await asyncio.sleep(0.05)
    assert exp.status == "recovered"
    assert 0.25 <= exp.recovery_seconds < 3


async def test_kill_that_never_heals_is_reported_failed():
    engine = _engine(FakeRuntime(heal_after=None), recovery_timeout_sec=0.5)
    exp = await engine.kill(service="payments")
    await asyncio.sleep(1.2)
    assert exp.status == "failed"


async def test_only_configured_targets_can_be_killed():
    engine = _engine(FakeRuntime())
    with pytest.raises(ChaosError):
        await engine.kill(service="postgres")


async def test_fault_injection_fans_out_to_every_ready_replica():
    engine = _engine(FakeRuntime())
    hits = []

    def handler(request):
        hits.append((request.url.host, request.headers["x-admin-token"]))
        return httpx.Response(200, json={"active": True})

    engine._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    exp = await engine.inject("orders", latency_ms=300, error_rate=0, duration_sec=30)
    assert exp.kind == "latency" and exp.status == "active"
    assert sorted(h[0] for h in hits) == ["10.0.0.0", "10.0.0.1"] and all(h[1] == "tok" for h in hits)


async def test_api_kill_alerts_and_token():
    from controlplane import main

    async with main.app.router.lifespan_context(main.app):
        main.ctx["chaos"] = _engine(FakeRuntime())
        main.ctx["runtime"] = main.ctx["chaos"].runtime
        main.settings.control_plane_token = "s3cret"
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            assert (await c.post("/api/chaos/kill", json={"service": "orders"})).status_code == 401
            r = await c.post("/api/chaos/kill", json={"service": "orders"}, headers={"X-PulseOps-Token": "s3cret"})
            assert r.status_code == 200 and r.json()["kind"] == "pod-kill"
            assert (await c.get("/api/experiments")).json()[0]["id"] == r.json()["id"]

            await c.post("/api/alerts/webhook", json={"alerts": [
                {"status": "firing", "fingerprint": "a1", "labels": {"alertname": "ServiceDown", "severity": "critical"},
                 "annotations": {"summary": "orders down"}, "startsAt": "2026-01-01T00:00:00Z"}]})
            alerts = (await c.get("/api/alerts")).json()
            assert alerts[0]["name"] == "ServiceDown" and alerts[0]["status"] == "firing"

            overview = (await c.get("/api/overview")).json()
            orders = next(s for s in overview["services"] if s["name"] == "orders")
            assert orders["desired"] == 2 and overview["alerts_firing"] == 1
        main.settings.control_plane_token = ""
