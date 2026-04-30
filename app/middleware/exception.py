"""
Smart exception handling middleware (pure ASGI implementation).

Renders HTML error pages for browser clients and JSON responses for API clients.
Includes a fallback to a static error page if template rendering itself fails.

Uses the pure ASGI interface instead of BaseHTTPMiddleware to avoid:
  - Full request body buffering
  - Broken streaming responses
  - Background task interference
"""

import structlog
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from config import settings

logger = structlog.get_logger(__name__)

templates = Jinja2Templates(directory="templates")


class SmartExceptionMiddleware:
    """Pure ASGI middleware for content-negotiated error handling."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def send_wrapper(message: dict) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            if response_started:
                # Headers already sent — we cannot replace the response.
                # Re-raise so Uvicorn logs the error and closes the connection.
                raise

            logger.error("Unhandled exception", error=str(exc), exc_info=True)
            response = self._build_error_response(Request(scope), exc)
            await response(scope, receive, send)

    @staticmethod
    def _build_error_response(request: Request, exc: Exception) -> Response:
        """Build the appropriate error response based on the Accept header."""
        status_code = 500
        detail = "Internal Server Error"
        if isinstance(exc, HTTPException):
            status_code = exc.status_code
            detail = exc.detail

        accept_header = request.headers.get("accept", "").lower()
        if "text/html" in accept_header:
            try:
                # Try to render the rich error template first
                template_name = "404.html" if status_code == 404 else "500.html"
                return templates.TemplateResponse(
                    template_name,
                    {"request": request, "detail": detail},
                    status_code=status_code,
                )
            except Exception as template_exc:
                # If templating fails, serve the static, self-contained error page
                logger.error("Template rendering failed", template=template_name, error=str(template_exc))
                return FileResponse("templates/error.html", status_code=500)

        # Fallback for non-HTML clients (APIs)
        return JSONResponse(
            status_code=status_code,
            content={"error": detail, "path": str(request.url), "version": settings.APP_VERSION},
        )
