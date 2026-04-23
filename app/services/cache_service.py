"""
Redis caching layer for headline data.

Implements cache-aside pattern with distributed locking to prevent
cache stampedes across multiple workers.
"""

import asyncio
import json
import logging
import time
from typing import List, Dict

import redis.asyncio as aioredis
import httpx

from config import settings
from metrics import CACHE_HITS, HEADLINES_FETCHED, FALLBACK_FETCHES, FALLBACK_REASON_ENUM
from services.feed_service import get_fresh_headlines

logger = logging.getLogger(__name__)

# Lua script for safe lock release — only deletes the lock if we still own it.
# Registered once at startup via register_lock_script() for Redis-side caching.
RELEASE_LOCK_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""

_release_lock = None  # Will hold the registered Script object


async def register_lock_script(redis_client: aioredis.Redis):
    """Register the Lua lock-release script with Redis for performance."""
    global _release_lock
    _release_lock = redis_client.register_script(RELEASE_LOCK_SCRIPT)


async def release_lock(redis_client: aioredis.Redis, lock_id: str):
    """Release the distributed lock, but only if we still own it."""
    if _release_lock:
        await _release_lock(keys=[settings.LOCK_KEY], args=[lock_id])
    else:
        # Fallback to eval if script wasn't registered
        await redis_client.eval(RELEASE_LOCK_SCRIPT, 1, settings.LOCK_KEY, lock_id)


async def fetch_and_cache_headlines(
    redis_client: aioredis.Redis,
    http_client: httpx.AsyncClient,
    response_class,
) -> dict:
    """Fetch live headlines, cache them in Redis, and return the API response dict."""
    logger.info("Fetching live headlines.")
    headlines = await get_fresh_headlines(http_client, redis_client)
    now = time.time()
    expires_at = now + settings.CACHE_TTL_SECONDS
    response_data = response_class(
        data=headlines,
        source="live",
        expires_at=expires_at,
        ttl=settings.CACHE_TTL_SECONDS,
    )
    await redis_client.setex(
        settings.CACHE_KEY,
        settings.CACHE_TTL_SECONDS,
        response_data.model_dump_json(),
    )
    HEADLINES_FETCHED.set(len(headlines))
    return response_data


async def wait_for_cache_or_fallback(
    redis_client: aioredis.Redis,
    http_client: httpx.AsyncClient,
    response_class,
) -> dict:
    """Wait for another worker to populate the cache, or perform a fallback fetch."""
    logger.info("Cache lock is held. Waiting for cache to be populated.")
    for _ in range(settings.LOCK_WAIT_TIMEOUT_SECONDS):
        await asyncio.sleep(1)
        cached = await redis_client.get(settings.CACHE_KEY)
        if cached:
            logger.info("Cache populated by another process. Serving new data.")
            CACHE_HITS.labels(source="wait_for_lock").inc()
            return response_class(**json.loads(cached))
    logger.warning("Timed out waiting for cache lock. Performing fallback fetch.")
    headlines = await get_fresh_headlines(http_client, redis_client)
    FALLBACK_FETCHES.labels(reason="lock_timeout").inc()
    FALLBACK_REASON_ENUM.state("lock_timeout")
    return response_class(data=headlines, source="fallback_nolock", expires_at=None)
