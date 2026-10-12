"""
Frontend routes.

GET /         — the main page (minimalist retro design), or JSON for API clients.
GET /archive, /retro — old URLs (the removed classic page, an evaluation URL); redirect to /.
"""

import secrets

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from config import ALL_VIEW, CATALOG, PAYWALLED_HOSTS, settings
from deps import limiter
from services.geo import region_for_country

router = APIRouter()

templates = Jinja2Templates(directory="templates")

# Region and topic tabs for the main page, in display order
_VIEWS = [{"id": ALL_VIEW, "label": "All"}] + [{"id": r.id, "label": r.label} for r in CATALOG.regions]
_TOPICS = [{"id": "everything", "label": "Everything"}] + [{"id": t.id, "label": t.label} for t in CATALOG.topics]


# The page's own inline script and style carry a per-response nonce; nothing else may run.
# Images: our own (icons, site icons); connections: our own API.
_CSP = (
    "default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; img-src 'self'; "
    "connect-src 'self'; manifest-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


@router.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
@limiter.limit(settings.API_RATE_LIMIT)
async def root(request: Request):
    accept_header = request.headers.get("accept", "")
    if "text/html" in accept_header or request.method == "HEAD":
        nonce = secrets.token_urlsafe(16)
        response = templates.TemplateResponse(
            request, "retro.html", {"views": _VIEWS, "topics": _TOPICS, "paywalled": PAYWALLED_HOSTS, "csp_nonce": nonce,
             # Starting tab for visitors who haven't chosen one (country from Cloudflare)
             "home_region": region_for_country(request.headers.get("cf-ipcountry"))},
        )
        response.headers["Content-Security-Policy"] = _CSP.format(nonce=nonce)
        return response
    return {
        "message": "News Aggregator API is running.",
        "version": settings.APP_VERSION,
        "docs": "/docs" if settings.ENABLE_API_DOCS else "disabled",
    }


@router.get("/archive", include_in_schema=False)
@router.get("/retro", include_in_schema=False)
async def old_url_redirect():
    return RedirectResponse("/", status_code=301)
