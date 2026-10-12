"""
Frontend routes.

GET /         — the main page (minimalist retro design), or JSON for API clients.
GET /archive  — the previous ("classic") page, kept temporarily for comparison.
GET /retro    — old evaluation URL; redirects to /.
"""

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from config import ALL_VIEW, CATALOG, settings
from deps import limiter

router = APIRouter()

templates = Jinja2Templates(directory="templates")

# Region and topic tabs for the main page, in display order
_VIEWS = [{"id": ALL_VIEW, "label": "All"}] + [{"id": r.id, "label": r.label} for r in CATALOG.regions]
_TOPICS = [{"id": "everything", "label": "Everything"}] + [{"id": t.id, "label": t.label} for t in CATALOG.topics]


@router.get("/", include_in_schema=False)
@limiter.limit(settings.API_RATE_LIMIT)
async def root(request: Request):
    accept_header = request.headers.get("accept", "")
    if "text/html" in accept_header:
        return templates.TemplateResponse(request, "retro.html", {"views": _VIEWS, "topics": _TOPICS})
    return {
        "message": "News Aggregator API is running.",
        "version": settings.APP_VERSION,
        "docs": "/docs" if settings.ENABLE_API_DOCS else "disabled",
    }


@router.get("/archive", include_in_schema=False)
@limiter.limit(settings.API_RATE_LIMIT)
async def archive(request: Request):
    return templates.TemplateResponse(request, "index.html")


@router.get("/retro", include_in_schema=False)
async def retro_redirect():
    return RedirectResponse("/", status_code=301)
