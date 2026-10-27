"""Payments service: a card-processor simulator with realistic latency and
a configurable decline/error rate, so there is always some failure signal
for the dashboards and alerts to show.

Captures are recorded in a small ledger table with order_id as the primary
key: a retried charge (from any replica) returns the original payment and
can never capture the same order twice.

/void is the saga's compensation for a charge whose outcome the caller
never learned (it timed out, the connection dropped): it refunds a capture
made by that saga attempt, and leaves a tombstone so the same attempt's
charge — possibly still sleeping inside a slow replica — can no longer
capture when it finally runs. A later attempt of the same order (a resumed
saga) is allowed to capture again.
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
    # saga attempt that captured (or voided) this payment
    "ALTER TABLE payments ADD COLUMN IF NOT EXISTS attempt_id UUID",
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
    attempt_id: uuid.UUID | None = None


class VoidRequest(BaseModel):
    order_id: uuid.UUID
    attempt_id: uuid.UUID | None = None


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


async def _existing(order_id: uuid.UUID):
    async with state["pool"].acquire() as conn:
        return await conn.fetchrow(
            "SELECT order_id, payment_id, status, attempt_id FROM payments WHERE order_id = $1", order_id
        )


def _response(row) -> ChargeResponse:
    return ChargeResponse(order_id=row["order_id"], payment_id=row["payment_id"], status=row["status"])


def _voided_for(row, attempt_id: uuid.UUID | None) -> bool:
    """True if this charge belongs to an attempt that was already voided."""
    return row["status"] == "voided" and row["attempt_id"] == attempt_id


@app.post("/charge", response_model=ChargeResponse)
async def charge(body: ChargeRequest) -> ChargeResponse:
    if (done := await _existing(body.order_id)) is not None:
        if done["status"] == "captured":
            PAYMENTS.labels("duplicate").inc()
            return _response(done)
        if _voided_for(done, body.attempt_id):
            PAYMENTS.labels("voided").inc()
            raise HTTPException(status_code=409, detail="payment attempt was voided")
        # voided by an earlier attempt: this is a resumed saga, charge again

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
        # Capture unless someone got there first. Overwriting is only
        # allowed for a tombstone left by a *different* attempt; the guard
        # sits in the statement itself, so a void that lands while this
        # request was sleeping still wins.
        row = await conn.fetchrow(
            "INSERT INTO payments (order_id, payment_id, amount_cents, status, attempt_id) "
            "VALUES ($1, $2, $3, 'captured', $4) "
            "ON CONFLICT (order_id) DO UPDATE SET payment_id = excluded.payment_id, "
            "amount_cents = excluded.amount_cents, status = 'captured', attempt_id = excluded.attempt_id, "
            "created_at = now() "
            "WHERE payments.status = 'voided' AND payments.attempt_id IS DISTINCT FROM excluded.attempt_id "
            "RETURNING order_id, payment_id, status, attempt_id",
            body.order_id, uuid.uuid4(), body.amount_cents, body.attempt_id,
        )
    if row is None:
        current = await _existing(body.order_id)
        if current["status"] == "captured":
            # a concurrent retry captured it first — return that payment
            PAYMENTS.labels("duplicate").inc()
            return _response(current)
        PAYMENTS.labels("voided").inc()
        raise HTTPException(status_code=409, detail="payment attempt was voided")
    PAYMENTS.labels("captured").inc()
    return _response(row)


@app.post("/void", response_model=ChargeResponse)
async def void(body: VoidRequest) -> ChargeResponse:
    """Compensation: refund this attempt's capture, or block it from ever
    happening. Idempotent. A capture made by a *different* attempt is left
    alone — that attempt is the one that owns the order now."""
    async with state["pool"].acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO payments (order_id, payment_id, amount_cents, status, attempt_id) "
            "VALUES ($1, $2, 0, 'voided', $3) "
            "ON CONFLICT (order_id) DO UPDATE SET status = 'voided', attempt_id = excluded.attempt_id "
            "WHERE payments.status = 'voided' OR payments.attempt_id IS NOT DISTINCT FROM excluded.attempt_id "
            "RETURNING order_id, payment_id, status, attempt_id",
            body.order_id, uuid.uuid4(), body.attempt_id,
        )
    if row is None:
        current = await _existing(body.order_id)
        PAYMENTS.labels("void_skipped").inc()
        return _response(current)
    PAYMENTS.labels("voided").inc()
    logger.info("payment voided", extra=log_fields(order_id=str(body.order_id)))
    return _response(row)
