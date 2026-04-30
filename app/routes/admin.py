"""
Admin API routes.

POST /admin/cache/clear — clears the Redis headline cache (requires API key).
"""

import structlog
from fastapi import APIRouter, Request, Security

from config import settings
from deps import get_api_key

logger = structlog.get_logger(__name__)

router = APIRouter()


@router.post("/admin/cache/clear", dependencies=[Security(get_api_key)], include_in_schema=False)
async def clear_cache(request: Request):
    redis_client = request.app.state.redis
    await redis_client.delete(settings.CACHE_KEY)
    logger.info("Admin request: Redis cache cleared successfully")
    return {"status": "ok", "message": "Cache cleared"}
