"""
Smart exception handling middleware.

Renders HTML error pages for browser clients and JSON responses for API clients.
Includes a fallback to a static error page if template rendering itself fails.
"""

import logging

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from config import settings

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory="templates")


class SmartExceptionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
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
                try:
                    # Try to render the rich error template first
                    template_name = "500.html"
                    if status_code == 404:
                        template_name = "404.html"
                    return templates.TemplateResponse(
                        template_name,
                        {"request": request, "detail": detail},
                        status_code=status_code
                    )
                except Exception as template_exc:
                    # If templating fails, serve the static, self-contained error page
                    logger.error(f"Failed to render template {template_name}: {template_exc}", exc_info=True)
                    return FileResponse("templates/error.html", status_code=500)

            # Fallback for non-HTML clients (APIs)
            return JSONResponse(
                status_code=status_code,
                content={"error": detail, "path": str(request.url), "version": settings.APP_VERSION}
            )
