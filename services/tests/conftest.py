import os

# must be set before any service module is imported (settings are read at import)
os.environ.setdefault("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", "postgresql://pulseops:pulseops@localhost:5434/pulseops"))
os.environ.setdefault("FAULT_ADMIN_TOKEN", "test-token")
os.environ.setdefault("RUNTIME_MODE", "none")
os.environ.setdefault("LOG_LEVEL", "WARNING")

from contextlib import asynccontextmanager  # noqa: E402

import httpx  # noqa: E402
import pytest_asyncio  # noqa: E402


@asynccontextmanager
async def running(app):
    """Run the app's lifespan (DB pool, migrations, clients) and yield an
    httpx client wired straight to it."""
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest_asyncio.fixture
async def inventory_client():
    from inventory import main

    async with running(main.app) as client:
        async with main.state["pool"].acquire() as conn:
            await conn.execute("DELETE FROM reservations")
            await conn.execute("UPDATE products SET stock = 50")
        yield client, main


@pytest_asyncio.fixture
async def orders_client():
    from orders import main

    async with running(main.app) as client:
        async with main.state["pool"].acquire() as conn:
            await conn.execute("DELETE FROM orders")
        yield client, main


@pytest_asyncio.fixture
async def payments_client():
    from payments import main

    main.settings.decline_rate = 0.0
    main.settings.processor_error_rate = 0.0
    main.settings.min_latency_ms = main.settings.max_latency_ms = 0
    async with running(main.app) as client:
        async with main.state["pool"].acquire() as conn:
            await conn.execute("DELETE FROM payments")
        yield client, main
