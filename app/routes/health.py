"""
Health check route.

GET /health  — Docker/Caddy health probe. Fails (503) only when this worker
               can't serve headlines at all: Redis is down AND it has no
               in-memory snapshot. Redis down with a snapshot is "degraded"
               (200), so Caddy keeps routing to it. Staleness is reported but
               never fails the probe: older headlines beat taking the API offline.
"""

import time

import structlog
from fastapi import APIRouter, Request, Response

from catalog import ALL_VIEW
from config import settings

logger = structlog.get_logger(__name__)

router = APIRouter()

@router.api_route("/health", methods=["GET", "HEAD"], include_in_schema=False)
async def health_check(request: Request, response: Response):
    snapshot = await request.app.state.headlines_cache.get(ALL_VIEW)
    age = round(time.time() - snapshot.fetched_at) if snapshot else None
    headlines = {"age_seconds": age, "stale": age is None or age > settings.STALE_AFTER_SECONDS}
    try:
        redis_client = request.app.state.redis
        await redis_client.ping()
        return {"status": "healthy", "redis": True, "headlines": headlines, "version": settings.APP_VERSION}
    except Exception as e:
        logger.error("Health check: Redis connection error", error=str(e), exc_info=True)
        if snapshot is None:
            response.status_code = 503
            return {"status": "unhealthy", "redis": False, "headlines": headlines, "version": settings.APP_VERSION}
        return {"status": "degraded", "redis": False, "headlines": headlines, "version": settings.APP_VERSION}
