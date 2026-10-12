"""
Shared FastAPI dependencies for the News Aggregator API.
"""

import structlog
from fastapi import HTTPException, Request, Security
from fastapi.security import APIKeyHeader
from redis.backoff import NoBackoff
from redis.retry import Retry
from slowapi import Limiter
from slowapi.util import get_remote_address

from config import settings

logger = structlog.get_logger(__name__)



def get_real_ip(request: Request) -> str:
    """
    Returns the client IP for rate limiting.

    Caddy always overwrites X-Real-IP with the client IP it resolved from its
    trusted_proxies list, so the header can't be spoofed by clients. The app
    port is only reachable from inside the Docker network.
    """
    return request.headers.get("x-real-ip") or get_remote_address(request)


limiter = Limiter(
    key_func=get_real_ip,
    storage_uri=settings.REDIS_URL,
    # Short timeouts and no retries: limit checks are synchronous, so a slow or
    # down Redis must not stall the event loop
    storage_options={"socket_timeout": 0.5, "socket_connect_timeout": 0.5, "retry": Retry(NoBackoff(), 0)},
    # Keep serving (with per-process limits) if Redis is down
    in_memory_fallback_enabled=True,
    swallow_errors=True,
)

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=True)


async def get_api_key(api_key: str = Security(api_key_header)):
    """Validates the admin API key from the X-API-Key header."""
    if not settings.ADMIN_API_KEY or settings.ADMIN_API_KEY == "changeme":
        if settings.DEBUG:
            logger.warning("Admin action attempted, but ADMIN_API_KEY is not set")
        raise HTTPException(status_code=501, detail="Admin endpoint not configured")
    if api_key != settings.ADMIN_API_KEY:
        raise HTTPException(status_code=403, detail="Invalid or missing API Key")
    return api_key
