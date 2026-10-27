"""Orders service: the orchestrator of a small saga.

    create order (pending) -> reserve stock (inventory) -> charge (payments)
        payment ok        -> confirmed
        payment declined  -> release stock -> failed
        dependency down   -> void charge -> release stock -> failed (503 to the caller)

Every downstream call goes through ResilientClient (timeouts, retries with
jittered backoff, circuit breaker), and every step is idempotent on
order_id, so killing any pod mid-saga leaves no stock leaked and no order
charged twice once the retries land on a healthy replica.

Only one saga runs per order at a time: a run first takes a short lease on
the order row (atomically, in Postgres), so a gateway retry that arrives
while the original is still working gets 409 instead of running a second
saga that could undo the first one's work. If the replica running a saga is
killed, its lease simply expires and any replica's recovery loop resumes
the order — stuck "pending" orders heal themselves.
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime

from fastapi import FastAPI, HTTPException, Query
from prometheus_client import Counter, Histogram
from pydantic import BaseModel, Field

from pulseops_common import BaseServiceSettings, create_service_app
from pulseops_common.db import create_pool, ping, run_migrations
from pulseops_common.logging import log_fields
from pulseops_common.resilience import CircuitOpenError, ResilientClient, UpstreamError

logger = logging.getLogger("pulseops.orders")

ORDERS = Counter("orders_total", "Orders by final status", ["status", "reason"])
SAGA_SECONDS = Histogram(
    "orders_saga_duration_seconds", "End-to-end order placement time",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
)
RECOVERED = Counter("orders_recovered_total", "Stuck pending orders resumed by the recovery loop")
COMPENSATION_FAILURES = Counter(
    "orders_compensation_failures_total", "Compensating actions that could not be applied", ["action"]
)

MIGRATIONS = [
    """CREATE TABLE IF NOT EXISTS orders (
        id UUID PRIMARY KEY,
        status TEXT NOT NULL,
        reason TEXT,
        items JSONB NOT NULL,
        total_cents INTEGER,
        payment_id UUID,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX IF NOT EXISTS idx_orders_created ON orders (created_at DESC)",
    # saga ownership: which run owns the order, and until when
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS attempt_id UUID",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS lease_until TIMESTAMPTZ",
    "CREATE INDEX IF NOT EXISTS idx_orders_pending_lease ON orders (lease_until) WHERE status = 'pending'",
]

# orders a retry (or the recovery loop) may pick up again
_RESUMABLE = "(status = 'pending' OR (status = 'failed' AND reason LIKE '%unavailable'))"


class Settings(BaseServiceSettings):
    database_url: str = "postgresql://pulseops:pulseops@localhost:5432/pulseops"
    inventory_url: str = "http://inventory:8000"
    payments_url: str = "http://payments:8000"
    # longer than the slowest possible saga (two dependencies, each with
    # retries and 2s timeouts), so a live run never loses its lease
    saga_lease_sec: float = 30.0
    recovery_interval_sec: float = 10.0


class OrderItem(BaseModel):
    sku: str = Field(min_length=1, max_length=32)
    qty: int = Field(ge=1, le=100)


class OrderCreate(BaseModel):
    items: list[OrderItem] = Field(min_length=1, max_length=20)
    # lets a client retry a POST safely: same key -> same order
    idempotency_key: uuid.UUID | None = None


class Order(BaseModel):
    id: uuid.UUID
    status: str
    reason: str | None
    items: list[OrderItem]
    total_cents: int | None
    payment_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


settings = Settings()
state: dict = {"pool": None, "inventory": None, "payments": None}


async def _recover_stale_forever() -> None:
    while True:
        await asyncio.sleep(settings.recovery_interval_sec)
        try:
            await recover_stale_orders()
        except Exception:
            logger.exception("order recovery failed")


async def lifespan(app: FastAPI):
    pool = await create_pool(settings.database_url)
    await run_migrations(pool, 0x4F5244, MIGRATIONS)
    state["pool"] = pool
    state["inventory"] = ResilientClient("orders", "inventory", settings.inventory_url)
    state["payments"] = ResilientClient("orders", "payments", settings.payments_url)
    task = asyncio.create_task(_recover_stale_forever())
    yield
    task.cancel()
    await state["inventory"].aclose()
    await state["payments"].aclose()
    await pool.close()
    state["pool"] = None


app = create_service_app(
    "orders", settings=settings, lifespan=lifespan, readiness_check=ping(lambda: state["pool"])
)


def _row_to_order(row) -> Order:
    data = dict(row)
    data["items"] = json.loads(data["items"]) if isinstance(data["items"], str) else data["items"]
    return Order(**data)


async def _get(order_id: uuid.UUID) -> Order | None:
    async with state["pool"].acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM orders WHERE id = $1", order_id)
    return _row_to_order(row) if row else None


async def _claim(order_id: uuid.UUID, items: list[dict] | None = None):
    """Take the saga lease. Returns (attempt_id, row) on success, or
    (None, current row) when the order is final or another run owns it.
    `items` creates the order if it doesn't exist yet."""
    attempt = uuid.uuid4()
    async with state["pool"].acquire() as conn:
        if items is not None:
            row = await conn.fetchrow(
                "INSERT INTO orders (id, status, items, attempt_id, lease_until) "
                "VALUES ($1, 'pending', $2, $3, now() + make_interval(secs => $4)) "
                "ON CONFLICT (id) DO NOTHING RETURNING *",
                order_id, json.dumps(items), attempt, settings.saga_lease_sec,
            )
            if row is not None:
                return attempt, row
        # A retry of an order that is still pending (its first run died
        # with a killed pod) or that failed only because a dependency was
        # unavailable is *resumed*, not replayed: every saga step is
        # idempotent, so re-running it is safe, and it lets a retry that
        # lands on a healthy replica actually succeed. Replaying the stored
        # failure would also hide real outages from callers (and from the
        # customer-impact alert). Final outcomes are returned as-is.
        row = await conn.fetchrow(
            "UPDATE orders SET status = 'pending', reason = NULL, attempt_id = $2, "
            "lease_until = now() + make_interval(secs => $3), updated_at = now() "
            f"WHERE id = $1 AND {_RESUMABLE} AND (lease_until IS NULL OR lease_until < now()) RETURNING *",
            order_id, attempt, settings.saga_lease_sec,
        )
        if row is not None:
            return attempt, row
        return None, await conn.fetchrow("SELECT * FROM orders WHERE id = $1", order_id)


async def _finish(
    order_id: uuid.UUID, attempt: uuid.UUID, status: str, reason: str | None = None, **fields
) -> Order:
    sets = ["status = $3", "reason = $4", "updated_at = now()", "lease_until = NULL"]
    args: list = [order_id, attempt, status, reason]
    for name, value in fields.items():
        args.append(value)
        sets.append(f"{name} = ${len(args)}")
    async with state["pool"].acquire() as conn:
        # only the run that holds the order may write its outcome
        row = await conn.fetchrow(
            f"UPDATE orders SET {', '.join(sets)} WHERE id = $1 AND attempt_id = $2 RETURNING *", *args
        )
        if row is None:
            logger.warning("saga lost its lease; outcome discarded", extra=log_fields(order_id=str(order_id)))
            row = await conn.fetchrow("SELECT * FROM orders WHERE id = $1", order_id)
            return _row_to_order(row)
    ORDERS.labels(status, reason or "none").inc()
    return _row_to_order(row)


async def _release(order_id: uuid.UUID) -> None:
    try:
        await state["inventory"].request("POST", "/release", json={"order_id": str(order_id)})
    except (UpstreamError, CircuitOpenError):
        # stock stays reserved; logged loudly so it can be reconciled
        COMPENSATION_FAILURES.labels("release").inc()
        logger.error("compensation failed: stock not released", extra=log_fields(order_id=str(order_id)))


async def _void(order_id: uuid.UUID, attempt: uuid.UUID) -> None:
    """A charge whose outcome we never learned may still have captured
    (the request timed out, not the payment): refund it, or make sure it
    can't capture later."""
    try:
        await state["payments"].request(
            "POST", "/void", json={"order_id": str(order_id), "attempt_id": str(attempt)}
        )
    except (UpstreamError, CircuitOpenError):
        # the order stays "failed: payments_unavailable", which a retry
        # resumes — and payments then returns the existing capture
        COMPENSATION_FAILURES.labels("void").inc()
        logger.error("compensation failed: payment not voided", extra=log_fields(order_id=str(order_id)))


async def _run_saga(order_id: uuid.UUID, attempt: uuid.UUID, items: list[dict]) -> Order:
    with SAGA_SECONDS.time():
        # 1. reserve stock (idempotent on order_id, so retries are safe)
        try:
            reserve = await state["inventory"].request(
                "POST", "/reserve", json={"order_id": str(order_id), "items": items}
            )
        except (UpstreamError, CircuitOpenError) as exc:
            await _release(order_id)  # a reserve may have committed before the failure
            await _finish(order_id, attempt, "failed", "inventory_unavailable")
            raise HTTPException(status_code=503, detail=f"inventory unavailable: {exc}") from None
        if reserve.status_code in (404, 409):
            return await _finish(order_id, attempt, "rejected", reserve.json().get("detail", "out of stock"))
        if not reserve.is_success:
            await _release(order_id)
            return await _finish(order_id, attempt, "failed", f"inventory_error_{reserve.status_code}")
        total = reserve.json()["total_cents"]

        # 2. charge (payments dedupes on order_id)
        try:
            payment = await state["payments"].request(
                "POST", "/charge",
                json={"order_id": str(order_id), "amount_cents": total, "attempt_id": str(attempt)},
            )
        except (UpstreamError, CircuitOpenError) as exc:
            await _void(order_id, attempt)
            await _release(order_id)
            await _finish(order_id, attempt, "failed", "payments_unavailable", total_cents=total)
            raise HTTPException(status_code=503, detail=f"payments unavailable: {exc}") from None
        if payment.status_code == 402:
            await _release(order_id)
            return await _finish(order_id, attempt, "failed", "card_declined", total_cents=total)
        if not payment.is_success:
            await _release(order_id)
            return await _finish(
                order_id, attempt, "failed", f"payment_error_{payment.status_code}", total_cents=total
            )

        return await _finish(
            order_id, attempt, "confirmed", None,
            total_cents=total, payment_id=uuid.UUID(payment.json()["payment_id"]),
        )


@app.post("/orders", response_model=Order, status_code=201)
async def create_order(body: OrderCreate) -> Order:
    order_id = body.idempotency_key or uuid.uuid4()
    attempt, row = await _claim(order_id, [i.model_dump() for i in body.items])
    if attempt is None:
        current = _row_to_order(row)
        if current.status == "pending":
            raise HTTPException(status_code=409, detail="order is already being processed; retry later")
        return current
    # a resumed order keeps the items it was created with
    return await _run_saga(order_id, attempt, [i.model_dump() for i in _row_to_order(row).items])


async def recover_stale_orders(limit: int = 20) -> int:
    """Resume pending orders whose saga run vanished (its pod was killed).
    Safe on every replica at once: each order is claimed atomically."""
    async with state["pool"].acquire() as conn:
        ids = await conn.fetch(
            "SELECT id FROM orders WHERE status = 'pending' AND (lease_until IS NULL OR lease_until < now()) "
            "ORDER BY created_at LIMIT $1",
            limit,
        )
    recovered = 0
    for record in ids:
        attempt, row = await _claim(record["id"])
        if attempt is None:
            continue
        recovered += 1
        RECOVERED.inc()
        logger.info("resuming stuck order", extra=log_fields(order_id=str(record["id"])))
        try:
            await _run_saga(record["id"], attempt, [i.model_dump() for i in _row_to_order(row).items])
        except HTTPException:
            pass  # recorded as failed (dependency unavailable); a client retry can resume it
    return recovered


@app.get("/orders/{order_id}", response_model=Order)
async def get_order(order_id: uuid.UUID) -> Order:
    order = await _get(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return order


@app.get("/orders", response_model=list[Order])
async def list_orders(limit: int = Query(20, ge=1, le=100)) -> list[Order]:
    async with state["pool"].acquire() as conn:
        rows = await conn.fetch("SELECT * FROM orders ORDER BY created_at DESC LIMIT $1", limit)
    return [_row_to_order(r) for r in rows]
