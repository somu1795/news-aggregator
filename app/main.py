"""
News Aggregator — FastAPI Application Entry Point.

This module is responsible only for:
  - Creating the FastAPI application instance
  - Wiring middleware (CORS, GZip, TrustedHost, exception handling, rate limiting)
  - Managing the application lifespan (startup/shutdown)
  - Including route modules
  - Mounting static file directories
"""

from contextlib import asynccontextmanager

import httpx
import redis.asyncio as aioredis
import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from prometheus_fastapi_instrumentator import Instrumentator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

from config import settings
from logging_config import configure_logging
from middleware.exception import SmartExceptionMiddleware
from services.cache_service import register_lock_script

# Configure structured logging before any logger is used
configure_logging(settings.LOG_LEVEL)

# Import metrics module so Prometheus collectors are registered at import time
import metrics as _metrics  # noqa: F401

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Lifespan — replaces deprecated @app.on_event("startup") / ("shutdown")
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manages application startup and shutdown resources."""
    # --- Startup ---
    logger.info("Initializing Redis connection pool")
    try:
        redis_pool = aioredis.ConnectionPool.from_url(
            url=settings.REDIS_URL,
            max_connections=30,
            decode_responses=True,
        )
        app.state.redis = aioredis.Redis.from_pool(redis_pool)

        limits = httpx.Limits(max_connections=50, max_keepalive_connections=20)
        app.state.http_client = httpx.AsyncClient(
            http2=True, timeout=settings.REQUEST_TIMEOUT, limits=limits,
        )

        await app.state.redis.ping()
        app.state.release_lock_script = register_lock_script(app.state.redis)
        logger.info("Redis connection pool initialized successfully")
    except Exception as e:
        logger.critical("Failed to connect to Redis during startup", error=str(e), exc_info=True)
        raise

    yield

    # --- Shutdown ---
    logger.info("Closing connections")
    if hasattr(app.state, "redis") and app.state.redis:
        await app.state.redis.close()
    if hasattr(app.state, "http_client") and app.state.http_client:
        await app.state.http_client.aclose()
    logger.info("Connections closed")


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="News Aggregator API",
    description="Production-ready news aggregation service",
    docs_url="/docs" if settings.ENABLE_API_DOCS else None,
    redoc_url=None,
    version=settings.APP_VERSION,
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Rate Limiting (slowapi)
# ---------------------------------------------------------------------------

def get_real_ip(request: Request) -> str:
    """Extracts the true client IP, prioritizing Cloudflare's header."""
    if "cf-connecting-ip" in request.headers:
        return request.headers["cf-connecting-ip"]
    if "x-real-ip" in request.headers:
        return request.headers["x-real-ip"]
    return get_remote_address(request)

limiter = Limiter(
    key_func=get_real_ip,
    storage_uri=settings.REDIS_URL,
    default_limits=[settings.API_RATE_LIMIT],
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)


# ---------------------------------------------------------------------------
# Middleware Stack (order matters — last added = first executed)
# ---------------------------------------------------------------------------

app.add_middleware(GZipMiddleware)

# CORS — restrict cross-origin access to the configured allowed hosts
app.add_middleware(
    CORSMiddleware,
    allow_origins=[f"https://{h}" for h in settings.allowed_hosts_list if h != "*"] or ["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts_list)
app.add_middleware(SmartExceptionMiddleware)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

from routes.headlines import router as headlines_router
from routes.admin import router as admin_router
from routes.health import router as health_router
from routes.frontend import router as frontend_router

app.include_router(headlines_router)
app.include_router(admin_router)
app.include_router(health_router)
app.include_router(frontend_router)


# ---------------------------------------------------------------------------
# Prometheus Instrumentation
# ---------------------------------------------------------------------------

Instrumentator(
    should_group_status_codes=True,
    should_ignore_untemplated=True,
    should_respect_env_var=True,
    should_instrument_requests_inprogress=True,
    excluded_handlers=[".*admin.*"],
).instrument(app)


# ---------------------------------------------------------------------------
# Static Files
# ---------------------------------------------------------------------------

app.mount("/modern/static", StaticFiles(directory="modern_static"), name="modern_static")
