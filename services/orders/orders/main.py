"""Orders service: the orchestrator of a small saga.

    create order (pending) -> reserve stock (inventory) -> charge (payments)
        payment ok        -> confirmed
        payment declined  -> release stock -> failed
        dependency down   -> release stock -> failed (503 to the caller)

Every downstream call goes through ResilientClient (timeouts, retries with
jittered backoff, circuit breaker), and every step is idempotent on
order_id, so killing any pod mid-saga leaves no stock leaked and no order
charged twice once the retries land on a healthy replica.
"""

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
]


class Settings(BaseServiceSettings):
    database_url: str = "postgresql://pulseops:pulseops@localhost:5432/pulseops"
    inventory_url: str = "http://inventory:8000"
    payments_url: str = "http://payments:8000"


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


async def lifespan(app: FastAPI):
    pool = await create_pool(settings.database_url)
    await run_migrations(pool, 0x4F5244, MIGRATIONS)
    state["pool"] = pool
    state["inventory"] = ResilientClient("orders", "inventory", settings.inventory_url)
    state["payments"] = ResilientClient("orders", "payments", settings.payments_url)
    yield
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


async def _finish(order_id: uuid.UUID, status: str, reason: str | None = None, **fields) -> Order:
    sets = ["status = $2", "reason = $3", "updated_at = now()"]
    args: list = [order_id, status, reason]
    for name, value in fields.items():
        args.append(value)
        sets.append(f"{name} = ${len(args)}")
    async with state["pool"].acquire() as conn:
        row = await conn.fetchrow(f"UPDATE orders SET {', '.join(sets)} WHERE id = $1 RETURNING *", *args)
    ORDERS.labels(status, reason or "none").inc()
    return _row_to_order(row)


async def _release(order_id: uuid.UUID) -> None:
    try:
        await state["inventory"].request("POST", "/release", json={"order_id": str(order_id)})
    except (UpstreamError, CircuitOpenError):
        # stock stays reserved; logged loudly so it can be reconciled
        logger.error("compensation failed: stock not released", extra=log_fields(order_id=str(order_id)))


@app.post("/orders", response_model=Order, status_code=201)
async def create_order(body: OrderCreate) -> Order:
    order_id = body.idempotency_key or uuid.uuid4()
    items = [i.model_dump() for i in body.items]
    existing = await _get(order_id)
    if existing is not None:
        # A retry of an order that is still pending (its first attempt died
        # with a killed pod) or that failed only because a dependency was
        # unavailable is *resumed*, not replayed: every saga step is
        # idempotent, so re-running it is safe, and it lets a retry that
        # lands on a healthy replica actually succeed. Replaying the stored
        # failure would also hide real outages from callers (and from the
        # customer-impact alert). Final outcomes are returned as-is.
        resumable = existing.status == "pending" or (
            existing.status == "failed" and (existing.reason or "").endswith("_unavailable")
        )
        if not resumable:
            return existing
        items = [i.model_dump() for i in existing.items]
        async with state["pool"].acquire() as conn:
            await conn.execute(
                "UPDATE orders SET status = 'pending', reason = NULL, updated_at = now() WHERE id = $1", order_id
            )
    else:
        async with state["pool"].acquire() as conn:
            await conn.execute(
                "INSERT INTO orders (id, status, items) VALUES ($1, 'pending', $2) ON CONFLICT (id) DO NOTHING",
                order_id, json.dumps(items),
            )

    with SAGA_SECONDS.time():
        # 1. reserve stock (idempotent on order_id, so retries are safe)
        try:
            reserve = await state["inventory"].request(
                "POST", "/reserve", json={"order_id": str(order_id), "items": items}
            )
        except (UpstreamError, CircuitOpenError) as exc:
            await _release(order_id)  # a reserve may have committed before the failure
            await _finish(order_id, "failed", "inventory_unavailable")
            raise HTTPException(status_code=503, detail=f"inventory unavailable: {exc}") from None
        if reserve.status_code in (404, 409):
            return await _finish(order_id, "rejected", reserve.json().get("detail", "out of stock"))
        total = reserve.json()["total_cents"]

        # 2. charge (payments dedupes on order_id)
        try:
            payment = await state["payments"].request(
                "POST", "/charge", json={"order_id": str(order_id), "amount_cents": total}
            )
        except (UpstreamError, CircuitOpenError) as exc:
            await _release(order_id)
            await _finish(order_id, "failed", "payments_unavailable", total_cents=total)
            raise HTTPException(status_code=503, detail=f"payments unavailable: {exc}") from None
        if payment.status_code == 402:
            await _release(order_id)
            return await _finish(order_id, "failed", "card_declined", total_cents=total)
        if not payment.is_success:
            await _release(order_id)
            return await _finish(order_id, "failed", f"payment_error_{payment.status_code}", total_cents=total)

        return await _finish(
            order_id, "confirmed", None, total_cents=total, payment_id=uuid.UUID(payment.json()["payment_id"])
        )


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
