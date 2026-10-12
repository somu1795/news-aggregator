"""
Redis client factory shared by the API and the refresher.
"""

from typing import Optional

import redis.asyncio as aioredis
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff

from config import settings


def create_redis_client(socket_timeout: Optional[float], retries: int) -> aioredis.Redis:
    """
    Creates a Redis client backed by a blocking pool, so bursts wait briefly
    for a free connection instead of failing, and a hung Redis can't stall
    callers past `socket_timeout`.

    `retries` is explicit because redis-py 8 defaults to 10 retries with
    backoff, which would turn a Redis outage into multi-second stalls.
    """
    pool = aioredis.BlockingConnectionPool.from_url(
        url=settings.REDIS_URL,
        max_connections=30,
        timeout=2,  # Max seconds to wait for a free connection
        socket_connect_timeout=2,
        socket_timeout=socket_timeout,
        retry=Retry(ExponentialBackoff(cap=1, base=0.1), retries),
        decode_responses=True,
    )
    return aioredis.Redis.from_pool(pool)
