import httpx

from pulseops_common import BaseServiceSettings, create_service_app


def _app(token="secret"):
    app = create_service_app(f"faulty-{token}", settings=BaseServiceSettings(fault_admin_token=token))

    @app.get("/work")
    async def work():
        return {"ok": True}

    return app


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_injected_errors_hit_traffic_but_not_probes():
    async with _client(_app()) as c:
        r = await c.post("/admin/faults", json={"error_rate": 1.0, "duration_sec": 30}, headers={"X-Admin-Token": "secret"})
        assert r.status_code == 200 and r.json()["active"]
        assert (await c.get("/work")).status_code == 503
        # probes must stay green so Kubernetes doesn't "heal" a simulated slow dependency
        assert (await c.get("/healthz")).status_code == 200
        assert (await c.get("/readyz")).status_code == 200
        await c.delete("/admin/faults", headers={"X-Admin-Token": "secret"})
        assert (await c.get("/work")).status_code == 200


async def test_fault_admin_requires_token_and_is_off_without_one():
    async with _client(_app()) as c:
        assert (await c.post("/admin/faults", json={"error_rate": 1.0})).status_code == 401
        assert (await c.post("/admin/faults", json={"error_rate": 1.0}, headers={"X-Admin-Token": "wrong"})).status_code == 401
    async with _client(_app(token="")) as c:
        assert (await c.post("/admin/faults", json={"error_rate": 1.0}, headers={"X-Admin-Token": ""})).status_code == 404


async def test_faults_expire_on_their_own(monkeypatch):
    app = _app("exp")
    async with _client(app) as c:
        await c.post("/admin/faults", json={"error_rate": 1.0, "duration_sec": 5}, headers={"X-Admin-Token": "exp"})
        import pulseops_common.faults as f

        real = f.time.monotonic
        monkeypatch.setattr(f.time, "monotonic", lambda: real() + 6)
        assert (await c.get("/work")).status_code == 200


async def test_metrics_endpoint_reports_red_metrics():
    async with _client(_app("m")) as c:
        await c.get("/work")
        body = (await c.get("/metrics")).text
        assert 'http_server_requests_total{method="GET",route="/work",service="faulty-m",status="200"}' in body
