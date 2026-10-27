"""Inventory service: product catalogue and stock reservations.

Reservations are atomic per order (all items or none) and idempotent per
order_id, so the orders service can safely retry a reserve/release call
that may or may not have landed before a pod was killed.
"""

import asyncio
import logging
import uuid

from fastapi import FastAPI, HTTPException
from prometheus_client import Counter, Gauge
from pydantic import BaseModel, Field

from pulseops_common import BaseServiceSettings, create_service_app
from pulseops_common.db import create_pool, ping, run_migrations
from pulseops_common.logging import log_fields

logger = logging.getLogger("pulseops.inventory")

RESERVATIONS = Counter("inventory_reservations_total", "Reservation attempts", ["outcome"])
STOCK = Gauge("inventory_stock_units", "Units in stock", ["sku"])

CATALOGUE = [
    ("KB-01", "Mechanical keyboard", 8900),
    ("MS-02", "Wireless mouse", 3900),
    ("MN-03", "27in monitor", 32900),
    ("HS-04", "Headset", 7900),
    ("DK-05", "USB-C dock", 14900),
]

MIGRATIONS = [
    """CREATE TABLE IF NOT EXISTS products (
        sku TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        price_cents INTEGER NOT NULL CHECK (price_cents >= 0),
        stock INTEGER NOT NULL CHECK (stock >= 0)
    )""",
    """CREATE TABLE IF NOT EXISTS reservations (
        order_id UUID NOT NULL,
        sku TEXT NOT NULL REFERENCES products(sku),
        qty INTEGER NOT NULL CHECK (qty > 0),
        released BOOLEAN NOT NULL DEFAULT false,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (order_id, sku)
    )""",
]


class Settings(BaseServiceSettings):
    database_url: str = "postgresql://pulseops:pulseops@localhost:5432/pulseops"
    restock_threshold: int = 20
    restock_to: int = 500
    restock_interval_sec: float = 15.0


class Product(BaseModel):
    sku: str
    name: str
    price_cents: int
    stock: int


class ReserveItem(BaseModel):
    sku: str = Field(min_length=1, max_length=32)
    qty: int = Field(ge=1, le=100)


class ReserveRequest(BaseModel):
    order_id: uuid.UUID
    items: list[ReserveItem] = Field(min_length=1, max_length=20)


class ReserveResponse(BaseModel):
    order_id: uuid.UUID
    total_cents: int


class ReleaseRequest(BaseModel):
    order_id: uuid.UUID


settings = Settings()
state: dict = {"pool": None}


async def _restock_forever() -> None:
    """Keeps the demo running forever: the load generator would otherwise
    drain stock and every order would fail with 409."""
    while True:
        await asyncio.sleep(settings.restock_interval_sec)
        try:
            async with state["pool"].acquire() as conn:
                rows = await conn.fetch(
                    "UPDATE products SET stock = $2 WHERE stock < $1 RETURNING sku",
                    settings.restock_threshold, settings.restock_to,
                )
                for row in await conn.fetch("SELECT sku, stock FROM products"):
                    STOCK.labels(row["sku"]).set(row["stock"])
            if rows:
                logger.info("restocked", extra=log_fields(skus=[r["sku"] for r in rows]))
        except Exception:
            logger.exception("restock failed")


async def lifespan(app: FastAPI):
    pool = await create_pool(settings.database_url)
    await run_migrations(pool, 0x494E56, MIGRATIONS)
    async with pool.acquire() as conn:
        await conn.executemany(
            "INSERT INTO products (sku, name, price_cents, stock) VALUES ($1, $2, $3, $4) ON CONFLICT (sku) DO NOTHING",
            [(sku, name, price, settings.restock_to) for sku, name, price in CATALOGUE],
        )
    state["pool"] = pool
    task = asyncio.create_task(_restock_forever())
    yield
    task.cancel()
    await pool.close()
    state["pool"] = None


app = create_service_app(
    "inventory", settings=settings, lifespan=lifespan, readiness_check=ping(lambda: state["pool"])
)


@app.get("/products", response_model=list[Product])
async def list_products() -> list[Product]:
    async with state["pool"].acquire() as conn:
        rows = await conn.fetch("SELECT sku, name, price_cents, stock FROM products ORDER BY sku")
    return [Product(**dict(r)) for r in rows]


async def _lock_order(conn, order_id: uuid.UUID) -> None:
    """Serialize reserve/release per order until the transaction ends."""
    await conn.execute("SELECT pg_advisory_xact_lock($1, hashtext($2))", 0x494E56, str(order_id))


@app.post("/reserve", response_model=ReserveResponse)
async def reserve(body: ReserveRequest) -> ReserveResponse:
    # merge duplicate skus so one order can't double-book a primary key
    wanted: dict[str, int] = {}
    for item in body.items:
        wanted[item.sku] = wanted.get(item.sku, 0) + item.qty

    async with state["pool"].acquire() as conn, conn.transaction():
        # two calls for the same order (a retry racing its slow original)
        # must not both pass the "already reserved?" check below
        await _lock_order(conn, body.order_id)
        existing = await conn.fetch(
            "SELECT r.sku, r.qty, p.price_cents FROM reservations r JOIN products p USING (sku) "
            "WHERE r.order_id = $1 AND NOT r.released",
            body.order_id,
        )
        if existing:
            # a retried call whose first attempt already committed
            RESERVATIONS.labels("duplicate").inc()
            return ReserveResponse(
                order_id=body.order_id, total_cents=sum(r["qty"] * r["price_cents"] for r in existing)
            )

        # a resumed order whose earlier attempt was compensated: drop the
        # released rows so the fresh reservation doesn't collide on the PK
        await conn.execute("DELETE FROM reservations WHERE order_id = $1 AND released", body.order_id)

        total = 0
        # lock rows in a fixed order so concurrent orders can't deadlock
        for sku in sorted(wanted):
            row = await conn.fetchrow(
                "UPDATE products SET stock = stock - $2 WHERE sku = $1 AND stock >= $2 "
                "RETURNING price_cents, stock",
                sku, wanted[sku],
            )
            if row is None:
                exists = await conn.fetchval("SELECT 1 FROM products WHERE sku = $1", sku)
                RESERVATIONS.labels("unknown_sku" if not exists else "insufficient_stock").inc()
                # raising rolls back every decrement made so far
                raise HTTPException(
                    status_code=404 if not exists else 409,
                    detail=f"unknown sku {sku}" if not exists else f"insufficient stock for {sku}",
                )
            await conn.execute(
                "INSERT INTO reservations (order_id, sku, qty) VALUES ($1, $2, $3)",
                body.order_id, sku, wanted[sku],
            )
            STOCK.labels(sku).set(row["stock"])
            total += row["price_cents"] * wanted[sku]

    RESERVATIONS.labels("reserved").inc()
    return ReserveResponse(order_id=body.order_id, total_cents=total)


@app.post("/release", status_code=204)
async def release(body: ReleaseRequest) -> None:
    """Compensation for a failed order: put reserved stock back. Idempotent."""
    async with state["pool"].acquire() as conn, conn.transaction():
        await _lock_order(conn, body.order_id)
        rows = await conn.fetch(
            "UPDATE reservations SET released = true WHERE order_id = $1 AND NOT released RETURNING sku, qty",
            body.order_id,
        )
        for row in rows:
            await conn.execute("UPDATE products SET stock = stock + $2 WHERE sku = $1", row["sku"], row["qty"])
    if rows:
        RESERVATIONS.labels("released").inc()
