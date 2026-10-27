"""Payments service: a card-processor simulator with realistic latency and
a configurable decline/error rate, so there is always some failure signal
for the dashboards and alerts to show.

Captures are recorded in a small ledger table with order_id as the primary
key: a retried charge (from any replica) returns the original payment and
can never capture the same order twice.
"""

import asyncio
import logging
import random
import uuid

from fastapi import FastAPI, HTTPException
from prometheus_client import Counter
from pydantic import BaseModel, Field

from pulseops_common import BaseServiceSettings, create_service_app
from pulseops_common.db import create_pool, ping, run_migrations
from pulseops_common.logging import log_fields

logger = logging.getLogger("pulseops.payments")

PAYMENTS = Counter("payments_processed_total", "Payment attempts", ["outcome"])

MIGRATIONS = [
    """CREATE TABLE IF NOT EXISTS payments (
        order_id UUID PRIMARY KEY,
        payment_id UUID NOT NULL,
        amount_cents INTEGER NOT NULL,
        status TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
]


class Settings(BaseServiceSettings):
    database_url: str = "postgresql://pulseops:pulseops@localhost:5432/pulseops"
    decline_rate: float = 0.03
    processor_error_rate: float = 0.01
    min_latency_ms: int = 20
    max_latency_ms: int = 150


class ChargeRequest(BaseModel):
    order_id: uuid.UUID
    amount_cents: int = Field(ge=1, le=10_000_000)


class ChargeResponse(BaseModel):
    order_id: uuid.UUID
    payment_id: uuid.UUID
    status: str


settings = Settings()
state: dict = {"pool": None}


async def lifespan(app: FastAPI):
    pool = await create_pool(settings.database_url)
    await run_migrations(pool, 0x504159, MIGRATIONS)
    state["pool"] = pool
    yield
    await pool.close()
    state["pool"] = None


app = create_service_app("payments", settings=settings, lifespan=lifespan, readiness_check=ping(lambda: state["pool"]))


async def _existing(order_id: uuid.UUID) -> ChargeResponse | None:
    async with state["pool"].acquire() as conn:
        row = await conn.fetchrow("SELECT order_id, payment_id, status FROM payments WHERE order_id = $1", order_id)
    return ChargeResponse(**dict(row)) if row else None


@app.post("/charge", response_model=ChargeResponse)
async def charge(body: ChargeRequest) -> ChargeResponse:
    if (done := await _existing(body.order_id)) is not None:
        PAYMENTS.labels("duplicate").inc()
        return done

    await asyncio.sleep(random.uniform(settings.min_latency_ms, settings.max_latency_ms) / 1000)

    roll = random.random()
    if roll < settings.processor_error_rate:
        PAYMENTS.labels("processor_error").inc()
        logger.warning("processor error", extra=log_fields(order_id=str(body.order_id)))
        raise HTTPException(status_code=503, detail="card processor unavailable")
    if roll < settings.processor_error_rate + settings.decline_rate:
        PAYMENTS.labels("declined").inc()
        raise HTTPException(status_code=402, detail="card declined")

    async with state["pool"].acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO payments (order_id, payment_id, amount_cents, status) VALUES ($1, $2, $3, 'captured') "
            "ON CONFLICT (order_id) DO NOTHING RETURNING order_id, payment_id, status",
            body.order_id, uuid.uuid4(), body.amount_cents,
        )
    if row is None:
        # a concurrent retry captured it first — return that payment
        PAYMENTS.labels("duplicate").inc()
        return await _existing(body.order_id)
    PAYMENTS.labels("captured").inc()
    return ChargeResponse(**dict(row))
