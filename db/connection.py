import asyncpg
import psycopg2
from config import settings

# ── Async pool (used by the agent tools) ──────────────────────
_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=settings.DATABASE_URL,
            min_size=2,
            max_size=10,
        )
    return _pool


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


# ── Sync connection (used for migrations / scripts) ───────────
def get_sync_connection():
    return psycopg2.connect(settings.DATABASE_URL)
