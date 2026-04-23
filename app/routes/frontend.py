"""
Frontend routes.

GET /        — serves the original Jinja-templated page (or JSON for API clients).
GET /modern  — serves the modern SPA frontend.
"""

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from fastapi.templating import Jinja2Templates

from config import settings

router = APIRouter()

templates = Jinja2Templates(directory="templates")


@router.get("/", include_in_schema=False)
async def root(request: Request):
    accept_header = request.headers.get("accept", "")
    if "text/html" in accept_header:
        return templates.TemplateResponse("index.html", {"request": request})
    return {
        "message": "News Aggregator API is running.",
        "version": settings.APP_VERSION,
        "docs": "/docs" if settings.ENABLE_API_DOCS else "disabled",
    }


@router.get("/modern", include_in_schema=False)
async def serve_modern_frontend():
    """Serves the modern SPA frontend's index.html."""
    return FileResponse("modern_static/index.html")
