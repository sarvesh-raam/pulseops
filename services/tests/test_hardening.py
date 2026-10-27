"""Regression tests for the hardening pass: one per fixed bug."""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

import httpx

from controlplane.chaos import ChaosEngine
from controlplane.runtime import Pod
from pulseops_common.logging import JsonFormatter
from pulseops_common.resilience import CircuitBreaker, ResilientClient

ITEMS = [{"sku": "KB-01", "qty": 1}]


def _captured(request):
    return httpx.Response(200, json={"payment_id": str(uuid.uuid4()), "status": "captured"})


def _wire(main, inventory_handler, payments_handler):
    main.state["inventory"] = ResilientClient(
        "orders", "inv-hard", "http://inventory", transport=httpx.MockTransport(inventory_handler), max_attempts=2
    )
    main.state["payments"] = ResilientClient(
        "orders", "pay-hard", "http://payments", transport=httpx.MockTransport(payments_handler), max_attempts=2
    )


def _reserve_ok(request):
    if request.url.path == "/reserve":
        return httpx.Response(200, json={"order_id": "x", "total_cents": 1000})
    return httpx.Response(204)


# --- payments: void compensation ----------------------------------------------


async def test_void_refunds_and_blocks_a_late_charge_of_the_same_attempt(payments_client):
    client, _ = payments_client
    order, attempt = str(uuid.uuid4()), str(uuid.uuid4())
    body = {"order_id": order, "amount_cents": 500, "attempt_id": attempt}
    assert (await client.post("/charge", json=body)).json()["status"] == "captured"

    voided = await client.post("/void", json={"order_id": order, "attempt_id": attempt})
    assert voided.json()["status"] == "voided"
    # the same attempt's charge, arriving late from a slow replica, must not re-capture
    assert (await client.post("/charge", json=body)).status_code == 409


async def test_void_before_charge_leaves_a_tombstone(payments_client):
    """The orders side timed out before payments even ran the charge."""
    client, _ = payments_client
    order, attempt = str(uuid.uuid4()), str(uuid.uuid4())
    await client.post("/void", json={"order_id": order, "attempt_id": attempt})
    late = await client.post("/charge", json={"order_id": order, "amount_cents": 500, "attempt_id": attempt})
    assert late.status_code == 409


async def test_resumed_attempt_can_capture_after_a_void(payments_client):
    client, _ = payments_client
    order = str(uuid.uuid4())
    await client.post("/void", json={"order_id": order, "attempt_id": str(uuid.uuid4())})
    retry = await client.post("/charge", json={"order_id": order, "amount_cents": 500, "attempt_id": str(uuid.uuid4())})
    assert retry.status_code == 200 and retry.json()["status"] == "captured"


async def test_void_leaves_another_attempts_capture_alone(payments_client):
    client, _ = payments_client
    order = str(uuid.uuid4())
    await client.post("/charge", json={"order_id": order, "amount_cents": 500, "attempt_id": str(uuid.uuid4())})
    r = await client.post("/void", json={"order_id": order, "attempt_id": str(uuid.uuid4())})
    assert r.json()["status"] == "captured"


# --- orders: saga ownership, compensation, recovery --------------------------


async def test_payment_timeout_voids_the_charge(orders_client):
    """A timed-out charge may still have captured: the saga must void it,
    or the customer pays for an order reported as failed."""
    client, main = orders_client
    paths = []

    def payments(request):
        paths.append(request.url.path)
        if request.url.path == "/charge":
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json={"order_id": "x", "payment_id": str(uuid.uuid4()), "status": "voided"})

    _wire(main, _reserve_ok, payments)
    r = await client.post("/orders", json={"items": ITEMS})
    assert r.status_code == 503
    assert paths[-1] == "/void"
    order = (await client.get("/orders?limit=1")).json()[0]
    assert order["status"] == "failed" and order["reason"] == "payments_unavailable"


async def test_retry_while_saga_runs_is_rejected_not_duplicated(orders_client):
    client, main = orders_client
    key = uuid.uuid4()
    async with main.state["pool"].acquire() as conn:
        await conn.execute(
            "INSERT INTO orders (id, status, items, attempt_id, lease_until) "
            "VALUES ($1, 'pending', $2, $3, now() + interval '30 seconds')",
            key, json.dumps(ITEMS), uuid.uuid4(),
        )
    charges = []
    _wire(main, _reserve_ok, lambda r: charges.append(1) or _captured(r))
    r = await client.post("/orders", json={"items": ITEMS, "idempotency_key": str(key)})
    assert r.status_code == 409 and charges == []


async def test_stuck_pending_order_is_recovered(orders_client):
    """Its saga died with a killed pod: once the lease lapses, the recovery
    loop finishes the order instead of leaving it pending forever."""
    client, main = orders_client
    key = uuid.uuid4()
    async with main.state["pool"].acquire() as conn:
        await conn.execute(
            "INSERT INTO orders (id, status, items, attempt_id, lease_until) "
            "VALUES ($1, 'pending', $2, $3, now() - interval '1 second')",
            key, json.dumps(ITEMS), uuid.uuid4(),
        )
    _wire(main, _reserve_ok, _captured)
    assert await main.recover_stale_orders() == 1
    assert (await client.get(f"/orders/{key}")).json()["status"] == "confirmed"


async def test_stale_run_cannot_overwrite_the_new_owners_outcome(orders_client):
    _, main = orders_client
    key, stale, owner = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with main.state["pool"].acquire() as conn:
        await conn.execute(
            "INSERT INTO orders (id, status, items, attempt_id) VALUES ($1, 'confirmed', $2, $3)",
            key, json.dumps(ITEMS), owner,
        )
    order = await main._finish(key, stale, "failed", "payments_unavailable")
    assert order.status == "confirmed"


async def test_unexpected_inventory_error_fails_the_order(orders_client):
    """Used to raise KeyError (500) and leave the order pending forever."""
    client, main = orders_client
    _wire(main, lambda r: httpx.Response(500, json={"error": "internal error"}), _captured)
    r = await client.post("/orders", json={"items": ITEMS})
    assert r.status_code == 201
    assert r.json()["status"] == "failed" and r.json()["reason"] == "inventory_error_500"


# --- inventory ------------------------------------------------------------------


async def test_concurrent_reserves_for_one_order_decrement_once(inventory_client):
    client, main = inventory_client
    body = {"order_id": str(uuid.uuid4()), "items": [{"sku": "MS-02", "qty": 3}]}
    results = await asyncio.gather(*(client.post("/reserve", json=body) for _ in range(5)))
    assert all(r.status_code == 200 for r in results)
    async with main.state["pool"].acquire() as conn:
        assert await conn.fetchval("SELECT stock FROM products WHERE sku = 'MS-02'") == 47


# --- resilience -------------------------------------------------------------------


def test_half_open_lets_only_one_trial_through(monkeypatch):
    breaker = CircuitBreaker("test", "single-trial", failure_threshold=1, reset_after_sec=5)
    breaker.record_failure()
    import pulseops_common.resilience as r

    real = r.time.monotonic
    monkeypatch.setattr(r.time, "monotonic", lambda: real() + 10)
    assert breaker.allow()  # the trial
    assert not breaker.allow() and not breaker.allow()  # everyone else still fails fast


# --- logging ----------------------------------------------------------------------------


def test_log_timestamps_are_utc():
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "hi", None, None)
    record.created = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc).timestamp()
    record.msecs = 0
    payload = json.loads(JsonFormatter("svc").format(record))
    assert payload["ts"] == "2026-01-01T12:00:00.000Z"


# --- chaos ------------------------------------------------------------------------


class StatefulSetRuntime:
    """A killed StatefulSet pod comes back under the same name (new uid)."""

    name = "kubernetes"

    def __init__(self):
        self.pods = [self._pod("old-uid")]

    @staticmethod
    def _pod(uid):
        return Pod(id=uid, name="postgres-0", service="postgres", status="Running", ready=True, restarts=0,
                   started_at=datetime.now(timezone.utc) - timedelta(minutes=5), ip="10.0.0.9")

    async def list_pods(self):
        return list(self.pods)

    async def desired_replicas(self):
        return {"postgres": 1}

    async def kill(self, pod):
        self.pods = []

        async def heal():
            await asyncio.sleep(0.2)
            self.pods = [self._pod("new-uid")]

        asyncio.get_running_loop().create_task(heal())

    async def aclose(self):
        pass


async def test_statefulset_pod_recovery_is_detected():
    engine = ChaosEngine(StatefulSetRuntime(), {"postgres"}, fault_token="t", poll_interval_sec=0.05)
    exp = await engine.kill(service="postgres")
    for _ in range(60):
        if exp.status != "running":
            break
        await asyncio.sleep(0.05)
    assert exp.status == "recovered"
