import os
import logging
import logging.config
import re
from fastapi import FastAPI, HTTPException, Request, Response, Security
import random
from fastapi.responses import JSONResponse
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response as StarletteResponse
from fastapi.middleware.trustedhost import TrustedHostMiddleware 
from prometheus_fastapi_instrumentator import Instrumentator
import feedparser
import redis.asyncio as redis
import asyncio
import time
import json
from typing import List, Dict, Optional
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
import httpx
from bs4 import BeautifulSoup
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST, Counter, Gauge
import config

logger = logging.getLogger(__name__)
APP_VERSION = "1.0.0"

ENABLE_API_DOCS = os.getenv("ENABLE_API_DOCS", "false").lower() in ("true", "1", "t")

app = FastAPI(
    title="News Aggregator API",
    description="Production-ready news aggregation service",
    docs_url="/docs" if ENABLE_API_DOCS else None,
    redoc_url=None,
    version=APP_VERSION
)

CACHE_HITS = Counter("headlines_cache_hits_total", "Total cache hits for headlines")
CACHE_MISSES = Counter("headlines_cache_misses_total", "Total cache misses for headlines")
HEADLINES_FETCHED = Gauge("headlines_fetched_count", "Number of headlines fetched in the last live pull")
REDIS_ERRORS = Counter("redis_errors_total", "Total number of Redis errors encountered")
FALLBACK_FETCHES = Counter("fallback_fetches_total", "Total number of fallback fetches performed")

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=True)
ADMIN_API_KEY_SECRET = os.getenv("ADMIN_API_KEY")

async def get_api_key(api_key: str = Security(api_key_header)):
    if not ADMIN_API_KEY_SECRET:
        logger.warning("Admin action attempted, but ADMIN_API_KEY is not set.")
        raise HTTPException(status_code=501, detail="Admin endpoint not configured")
    if api_key != ADMIN_API_KEY_SECRET:
        raise HTTPException(status_code=403, detail="Invalid or missing API Key")
    return api_key

templates = Jinja2Templates(directory="templates")

class SmartExceptionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> StarletteResponse:
        try:
            return await call_next(request)
        except Exception as exc:
            logger.error(f"An unhandled exception occurred: {exc}", exc_info=True)
            status_code = 500
            detail = "Internal Server Error"
            if isinstance(exc, HTTPException):
                status_code = exc.status_code
                detail = exc.detail
            accept_header = request.headers.get("accept", "").lower()
            if "text/html" in accept_header:
                template_name = "500.html"
                if status_code == 404:
                    template_name = "404.html"
                return templates.TemplateResponse(
                    template_name,
                    {"request": request, "detail": detail},
                    status_code=status_code
                )
            return JSONResponse(
                status_code=status_code,
                content={"error": detail, "path": str(request.url), "version": APP_VERSION}
            )

app.add_middleware(GZipMiddleware)

try:
    allowed_hosts_list = os.environ["ALLOWED_HOSTS"].split(",")
except KeyError:
    raise RuntimeError("ALLOWED_HOSTS environment variable not set. Please configure it.")

app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts_list)
app.add_middleware(SmartExceptionMiddleware)

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_MAX_CONNECTIONS = int(os.getenv("REDIS_MAX_CONNECTIONS", 100))
REDIS_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}"

@app.on_event("startup")
async def startup_event():
    logger.info("Initializing Redis connection pool...")
    try:
        redis_pool = redis.ConnectionPool.from_url(
            url=REDIS_URL,
            max_connections=REDIS_MAX_CONNECTIONS,
            socket_timeout=5,
            socket_connect_timeout=5,
            retry_on_timeout=True,
            decode_responses=True,
        )
        app.state.redis = redis.Redis.from_pool(redis_pool)
        app.state.http_client = httpx.AsyncClient(http2=True, timeout=config.REQUEST_TIMEOUT)
        await app.state.redis.ping()
        logger.info("Redis connection pool initialized successfully.")
    except Exception as e:
        logger.critical(f"Failed to connect to Redis during startup: {e}", exc_info=True)
        raise

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Closing Redis connection pool...")
    if hasattr(app.state, 'redis') and app.state.redis:
        await app.state.redis.close()
    if hasattr(app.state, 'http_client') and app.state.http_client:
        await app.state.http_client.aclose()
    logger.info("Redis connection pool closed.")

class HeadlineItem(BaseModel):
    title: str
    link: str
    source: str
    published: float

class APIResponse(BaseModel):
    data: List[HeadlineItem]
    source: str
    cached_until: Optional[float] = None

async def _resolve_redirect(client: httpx.AsyncClient, url: str) -> str:
    """
    Resolves a Google News RSS URL. It first attempts to follow standard redirects.
    If the URL is still on Google's domain, it scrapes the landing page and makes a
    secondary API call to get the final article URL.
    """
    # Define headers to mimic a browser, which is crucial for the initial GET request
    # to prevent Google from serving a simple 302 redirect.
    headers = {
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36',
    }
    try:
        # 1. Initial GET request, following redirects and using a browser User-Agent.
        initial_resp = await client.get(url, headers=headers, follow_redirects=True, timeout=10.0)
        initial_resp.raise_for_status()

        # If a simple redirect led to the final article, we're done.
        final_url = str(initial_resp.url)
        if "news.google.com" not in final_url:
            logger.debug(f"Successfully resolved {url} to {final_url} via standard redirect.")
            return final_url

        # 2. If still on Google, parse the HTML to find the hidden data for the API call.
        soup = BeautifulSoup(initial_resp.text, 'html.parser')
        c_wiz_element = soup.select_one('c-wiz[data-p]')
        if not c_wiz_element:
            logger.warning(f"Could not find c-wiz element for {url}. Falling back to last known URL: {final_url}")
            return final_url

        data_p = c_wiz_element.get('data-p')
        obj = json.loads(data_p.replace('%.@.', '["garturlreq",'))

        # 3. Construct the payload for the batchexecute API call.
        payload = {
            'f.req': json.dumps([[['Fbv4je', json.dumps(obj[:-6] + obj[-2:]), 'null', 'generic']]])
        }
        post_headers = {
            'content-type': 'application/x-www-form-urlencoded;charset=UTF-8',
            'user-agent': headers['user-agent'],
        }
        batch_url = "https://news.google.com/_/DotsSplashUi/data/batchexecute"

        # 4. Make the POST request to the batch execute endpoint.
        response = await client.post(batch_url, headers=post_headers, data=payload, timeout=10.0)
        response.raise_for_status()

        # 5. Parse the complex response to extract the final URL.
        array_string = json.loads(response.text.replace(")]}'", ""))[0][2]
        article_url = json.loads(array_string)[1]
        logger.debug(f"Successfully resolved {url} to {article_url} via batchexecute API.")
        return article_url

    except Exception as e:
        # This broad exception handles httpx errors, parsing errors, etc.
        logger.warning(f"Failed to resolve redirect for {url}: {e}. Falling back to original link.")
        return url

async def _fetch_and_parse_feed(client: httpx.AsyncClient, source_name: str, url: str) -> List[Dict]:
    """Fetches and parses a single RSS feed with retry logic."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "application/xml,application/xhtml+xml,text/html;q=0.9, text/plain;q=0.8,*/*;q=0.5",
        "Accept-Language": "en-US,en;q=0.5",
    }
    for attempt in range(config.FETCH_RETRIES):
        try:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            feed = feedparser.parse(response.content)
            if getattr(feed, 'bozo', False):
                raise ValueError(f"Feed parse error for {source_name}: {feed.bozo_exception}")
            
            headlines = []
            for entry in feed.entries:
                headlines.append({
                    "title": entry.title,
                    "link": entry.link,
                    # Prioritize the source from the article entry, fallback to the feed's name from config
                    "source": getattr(entry, 'source', {}).get('title') or source_name,
                    "published": time.mktime(entry.published_parsed) if hasattr(entry, 'published_parsed') else time.time()
                })
            logger.info(f"Successfully fetched {len(headlines)} headlines from {source_name} at {url}")
            return headlines
        except Exception as e:
            logger.warning(f"Attempt {attempt + 1} failed for {source_name} at {url}: {e}")
            if attempt < config.FETCH_RETRIES - 1:
                await asyncio.sleep(config.BACKOFF_BASE ** attempt)
            else:
                logger.error(f"Failed to fetch from {source_name} at {url} after {config.FETCH_RETRIES} attempts.", exc_info=True)
                return []
    return []

async def _get_fresh_headlines(client: httpx.AsyncClient) -> List[Dict]:
    """
    Fetches headlines from all configured news sources concurrently, aggregates them
    according to configured weights, shuffles them randomly, and resolves the
    final links for the top articles.
    """
    # 1. Fetch from all sources concurrently
    fetch_tasks = [
        _fetch_and_parse_feed(client, name, url)
        for name, url in config.NEWS_SOURCES.items()
    ]
    source_names = list(config.NEWS_SOURCES.keys())
    results_by_source_list = await asyncio.gather(*fetch_tasks)
    results_by_source = {name: headlines for name, headlines in zip(source_names, results_by_source_list)}

    # 2. Sort each source's headlines individually by date
    for name in results_by_source:
        results_by_source[name].sort(key=lambda x: x["published"], reverse=True)

    # 3. Allocate headlines based on weights from config
    weighted_headlines = []
    allocations = {name: round(config.MAX_HEADLINES * weight) for name, weight in config.SOURCE_WEIGHTS.items()}

    # Adjust for rounding errors to ensure the total is exactly MAX_HEADLINES
    total_allocated = sum(allocations.values())
    if total_allocated != config.MAX_HEADLINES:
        remainder = config.MAX_HEADLINES - total_allocated
        # Give remainder to the source with the highest weight
        primary_source = max(config.SOURCE_WEIGHTS, key=config.SOURCE_WEIGHTS.get)
        if primary_source in allocations:
            allocations[primary_source] += remainder

    # 4. Gather headlines according to the calculated allocations
    for source_name, num_to_take in allocations.items():
        source_headlines = results_by_source.get(source_name, [])
        weighted_headlines.extend(source_headlines[:num_to_take])

    # 5. Shuffle the final combined list for a random mix.
    random.shuffle(weighted_headlines)
    top_headlines = weighted_headlines

    if not top_headlines:
        logger.error("Failed to fetch any headlines from any source.")
        return []

    logger.info(f"Aggregated {len(top_headlines)} headlines from {len(config.NEWS_SOURCES)} sources with weighting.")

    # 6. Resolve redirects for the final list of headlines
    resolve_tasks = [_resolve_redirect(client, h['link']) for h in top_headlines]
    resolved_links = await asyncio.gather(*resolve_tasks)
    for i, headline in enumerate(top_headlines):
        headline['link'] = resolved_links[i]
    return top_headlines

async def _fetch_and_cache_headlines(redis_client: redis.Redis, http_client: httpx.AsyncClient) -> APIResponse:
    logger.info("Fetching live headlines.")
    headlines = await _get_fresh_headlines(http_client)
    expires_at = time.time() + config.CACHE_TTL_SECONDS
    response_data = APIResponse(data=headlines, source="live", cached_until=expires_at)
    await redis_client.setex(config.CACHE_KEY, config.CACHE_TTL_SECONDS, response_data.json())
    HEADLINES_FETCHED.set(len(headlines))
    return response_data

async def _wait_for_cache_or_fallback(redis_client: redis.Redis, http_client: httpx.AsyncClient) -> APIResponse:
    logger.info("Cache lock is held. Waiting for cache to be populated.")
    for _ in range(12):
        await asyncio.sleep(1)
        cached = await redis_client.get(config.CACHE_KEY)
        if cached:
            logger.info("Cache populated by another process. Serving new data.")
            return APIResponse(**json.loads(cached))
    logger.warning("Timed out waiting for cache lock. Performing fallback fetch.")
    headlines = await _get_fresh_headlines(http_client)
    FALLBACK_FETCHES.inc()
    return APIResponse(data=headlines, source="fallback_nolock", cached_until=None)

@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
async def serve_frontend(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/api/headlines", response_model=APIResponse)
async def get_headlines(request: Request):
    redis_client = request.app.state.redis
    http_client = request.app.state.http_client
    LOCK_KEY = f"{config.CACHE_KEY}:lock"
    LOCK_TIMEOUT_SECONDS = 15
    try:
        cached = await redis_client.get(config.CACHE_KEY)
        if cached:
            logger.info("Serving response from cache")
            CACHE_HITS.inc()
            return APIResponse(**json.loads(cached))
        is_lock_acquired = await redis_client.set(LOCK_KEY, "1", nx=True, ex=LOCK_TIMEOUT_SECONDS)
        if is_lock_acquired:
            logger.info("Cache miss and lock acquired.")
            CACHE_MISSES.inc()
            try:
                return await _fetch_and_cache_headlines(redis_client, http_client)
            finally:
                await redis_client.delete(LOCK_KEY)
        else:
            return await _wait_for_cache_or_fallback(redis_client, http_client)
    except redis.RedisError as e:
        logger.error(f"Redis error, performing fallback fetch: {e}")
        REDIS_ERRORS.inc()
        headlines = await _get_fresh_headlines(http_client)
        return APIResponse(data=headlines, source="fallback_redis_error", cached_until=None)
    except Exception as e:
        logger.error(f"Unexpected error in get_headlines: {e}", exc_info=True)
        raise HTTPException(status_code=503, detail="Service temporarily unavailable")

@app.post("/admin/cache/clear", dependencies=[Security(get_api_key)], include_in_schema=False)
async def clear_cache(request: Request):
    redis_client = request.app.state.redis
    await redis_client.delete(config.CACHE_KEY)
    logger.info("Admin request: Redis cache cleared successfully.")
    return {"status": "ok", "message": "Cache cleared"}

@app.get("/health", include_in_schema=False)
async def health_check(request: Request, response: Response):
    try:
        redis_client = request.app.state.redis
        await redis_client.ping()
        return {"status": "healthy", "redis": True, "version": APP_VERSION}
    except Exception as e:
        logger.error(f"Health check failed: Redis connection error: {e}", exc_info=True)
        response.status_code = 503
        return {"status": "unhealthy", "redis": False, "version": APP_VERSION}

instrumentator = Instrumentator(
    should_group_status_codes=True,
    should_ignore_untemplated=True,
    should_respect_env_var=True,
    should_instrument_requests_inprogress=True,
    excluded_handlers=[".*admin.*"]
).instrument(app)

@app.get("/metrics", include_in_schema=False)
async def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
