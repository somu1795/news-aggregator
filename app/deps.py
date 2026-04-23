"""
Shared FastAPI dependencies for the News Aggregator API.
"""

import logging

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader

from config import settings

logger = logging.getLogger(__name__)

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=True)


async def get_api_key(api_key: str = Security(api_key_header)):
    """Validates the admin API key from the X-API-Key header."""
    if not settings.ADMIN_API_KEY or settings.ADMIN_API_KEY == "changeme":
        if settings.DEBUG:
            logger.warning("Admin action attempted, but ADMIN_API_KEY is not set.")
        raise HTTPException(status_code=501, detail="Admin endpoint not configured")
    if api_key != settings.ADMIN_API_KEY:
        raise HTTPException(status_code=403, detail="Invalid or missing API Key")
    return api_key
