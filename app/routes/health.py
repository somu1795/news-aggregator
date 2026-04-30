"""
Health check and metrics routes.

GET /health  — Docker/orchestrator health probe.
GET /metrics — Prometheus-compatible metrics endpoint.
                NOTE: In production, restrict external access to /metrics
                via your reverse proxy (e.g., Caddy) rather than in
                application code, since the proxy's client IP is the only
                IP visible to the app inside Docker.
"""

import structlog
from fastapi import APIRouter, Request, Response
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

from config import settings

logger = structlog.get_logger(__name__)

router = APIRouter()


@router.get("/health", include_in_schema=False)
async def health_check(request: Request, response: Response):
    try:
        redis_client = request.app.state.redis
        await redis_client.ping()
        return {"status": "healthy", "redis": True, "version": settings.APP_VERSION}
    except Exception as e:
        logger.error("Health check failed: Redis connection error", error=str(e), exc_info=True)
        response.status_code = 503
        return {"status": "unhealthy", "redis": False, "version": settings.APP_VERSION}


@router.get("/metrics", include_in_schema=False)
async def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
