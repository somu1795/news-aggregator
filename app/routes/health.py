"""
Health check and metrics routes.

GET /health  — Docker/Caddy health probe. Fails (503) only when this worker
               can't serve headlines at all: Redis is down AND it has no
               in-memory snapshot. Redis down with a snapshot is "degraded"
               (200), so Caddy keeps routing to it. Staleness is reported but
               never fails the probe: older headlines beat taking the API offline.
GET /metrics — Prometheus-compatible metrics endpoint.
                NOTE: In production, restrict external access to /metrics
                via your reverse proxy (e.g., Caddy) rather than in
                application code, since the proxy's client IP is the only
                IP visible to the app inside Docker.
"""

import os
import time

import structlog
from fastapi import APIRouter, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, CollectorRegistry, generate_latest, multiprocess

from config import ALL_VIEW, settings

logger = structlog.get_logger(__name__)

router = APIRouter()

# With several Uvicorn workers, each writes its metrics to PROMETHEUS_MULTIPROC_DIR
# and a scrape must aggregate all of them, not just the worker that answered.
if "PROMETHEUS_MULTIPROC_DIR" in os.environ:
    _metrics_registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(_metrics_registry)
else:
    _metrics_registry = REGISTRY


@router.get("/health", include_in_schema=False)
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


@router.get("/metrics", include_in_schema=False)
async def metrics():
    return Response(content=generate_latest(_metrics_registry), media_type=CONTENT_TYPE_LATEST)
