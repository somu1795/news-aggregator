"""
News Aggregator — FastAPI Application Entry Point.

This module is responsible only for:
  - Creating the FastAPI application instance
  - Wiring middleware (CORS, TrustedHost, exception handling, rate limiting)
  - Managing the application lifespan (startup/shutdown)
  - Including route modules

Compression is handled by Caddy, and headline fetching by the separate
refresher process (refresher.py); this process only serves reads.
"""

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from prometheus_fastapi_instrumentator import Instrumentator
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from config import CATALOG, settings
from deps import limiter
from logging_config import configure_logging
from middleware.exception import SmartExceptionMiddleware
from services.headlines_cache import HeadlinesCache
from services.redis_client import create_redis_client

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
        app.state.redis = create_redis_client(socket_timeout=2, retries=0)  # Fail fast; the in-memory snapshot covers outages
        await app.state.redis.ping()
        app.state.headlines_cache = HeadlinesCache(app.state.redis, CATALOG.view_ids + CATALOG.topic_view_ids)
        # Load the snapshots now, so every worker can keep serving if Redis later goes down
        await app.state.headlines_cache.warm()
        logger.info("Redis connection pool initialized successfully")
    except Exception as e:
        logger.critical("Failed to connect to Redis during startup", error=str(e), exc_info=True)
        raise

    yield

    # --- Shutdown ---
    logger.info("Closing connections")
    if hasattr(app.state, "redis") and app.state.redis:
        await app.state.redis.aclose()
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
# Limits are applied per route with @limiter.limit (see deps.py). slowapi's
# SlowAPIMiddleware is not used: it can't see routes inside FastAPI's
# included routers and silently skips every request.

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# ---------------------------------------------------------------------------
# Middleware Stack (order matters — last added = first executed)
# ---------------------------------------------------------------------------

# CORS — restrict cross-origin access to the configured allowed hosts
app.add_middleware(
    CORSMiddleware,
    allow_origins=[f"https://{h}" for h in settings.allowed_hosts_list if h != "*"] or ["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts_list)
app.add_middleware(SmartExceptionMiddleware)
app.add_exception_handler(StarletteHTTPException, SmartExceptionMiddleware.http_exception_handler)


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
    should_instrument_requests_inprogress=True,
    excluded_handlers=[".*admin.*"],
).instrument(app)
