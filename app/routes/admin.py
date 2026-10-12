"""
Admin API routes.

POST /admin/cache/clear — asks the refresher to publish a fresh snapshot now
                          (requires API key). The current snapshot keeps being
                          served until the new one is ready.
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
    await redis_client.lpush(settings.REFRESH_REQUEST_KEY, "1")
    logger.info("Admin request: refresh requested")
    return {"status": "ok", "message": "Refresh requested"}
