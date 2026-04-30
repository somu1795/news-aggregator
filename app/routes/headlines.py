"""
Headlines API route.

GET /api/headlines — returns cached or freshly-fetched headlines with
distributed locking to prevent cache stampedes.
"""

import asyncio
import json
import uuid
from typing import List, Optional

import redis.asyncio as aioredis
import structlog
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from config import settings
from metrics import CACHE_HITS, CACHE_MISSES, REDIS_ERRORS, FALLBACK_FETCHES, FALLBACK_REASON_ENUM
from services.cache_service import fetch_and_cache_headlines, wait_for_cache_or_fallback, release_lock
from services.feed_service import get_fresh_headlines

logger = structlog.get_logger(__name__)

router = APIRouter()


class HeadlineItem(BaseModel):
    title: str
    link: str
    source: str
    published: float
    last_updated: float


class APIResponse(BaseModel):
    data: List[HeadlineItem]
    source: str
    expires_at: Optional[float] = None
    ttl: Optional[int] = None


@router.get("/api/headlines", response_model=APIResponse)
async def get_headlines(request: Request, response: Response):
    redis_client = request.app.state.redis
    http_client = request.app.state.http_client
    lock_script = request.app.state.release_lock_script
    lock_id = str(uuid.uuid4())

    try:
        cached, ttl = await asyncio.gather(
            redis_client.get(settings.CACHE_KEY),
            redis_client.ttl(settings.CACHE_KEY),
        )
        if cached:
            logger.info("Serving response from cache")
            CACHE_HITS.labels(source="redis").inc()
            response.headers["Cache-Control"] = f"public, max-age={ttl if ttl > 0 else 0}"

            api_response = APIResponse(**json.loads(cached))
            api_response.ttl = ttl
            return api_response

        is_lock_acquired = await redis_client.set(
            settings.LOCK_KEY, lock_id, nx=True, ex=settings.LOCK_TIMEOUT_SECONDS
        )
        if is_lock_acquired:
            logger.info("Cache miss and lock acquired")
            CACHE_MISSES.labels(reason="expired").inc()
            try:
                result = await fetch_and_cache_headlines(redis_client, http_client, APIResponse)
                response.headers["Cache-Control"] = "no-store"
                return result
            finally:
                await release_lock(redis_client, lock_id, lock_script)
        else:
            CACHE_MISSES.labels(reason="locked").inc()
            result = await wait_for_cache_or_fallback(redis_client, http_client, APIResponse)
            response.headers["Cache-Control"] = "no-store"
            return result
    except aioredis.RedisError as e:
        logger.error("Redis error, performing fallback fetch", error=str(e), exc_info=True)
        REDIS_ERRORS.labels(operation="get_headlines").inc()
        FALLBACK_FETCHES.labels(reason="redis_error").inc()
        FALLBACK_REASON_ENUM.state("redis_error")
        headlines = await get_fresh_headlines(http_client, redis_client)
        response.headers["Cache-Control"] = "no-store"
        return APIResponse(data=headlines, source="fallback_redis_error", expires_at=None)
    except Exception as e:
        logger.error("Unexpected error in get_headlines", error=str(e), exc_info=True)
        raise HTTPException(status_code=503, detail="Service temporarily unavailable")
