import asyncpg

from pulseops_common.resilience import retry_async


def normalize_dsn(url: str) -> str:
    """Accept SQLAlchemy-style URLs too (postgresql+asyncpg://...)."""
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def create_pool(dsn: str, *, min_size: int = 1, max_size: int = 10) -> asyncpg.Pool:
    # Postgres may still be starting (or restarting after a chaos kill):
    # retry with backoff instead of crash-looping the pod.
    return await retry_async(
        lambda: asyncpg.create_pool(normalize_dsn(dsn), min_size=min_size, max_size=max_size, command_timeout=5),
        attempts=10,
    )


async def run_migrations(pool: asyncpg.Pool, lock_key: int, statements: list[str]) -> None:
    """Idempotent schema bootstrap, serialized across replicas with an
    advisory lock so N pods starting together don't race on CREATE TABLE."""
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", lock_key)
        for statement in statements:
            await conn.execute(statement)


def ping(pool_getter):
    async def _ping() -> None:
        pool = pool_getter()
        if pool is None:
            raise RuntimeError("database pool not initialised")
        async with pool.acquire(timeout=1) as conn:
            await conn.fetchval("SELECT 1")

    return _ping
