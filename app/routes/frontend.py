"""
Frontend routes.

GET /         — the main page (minimalist retro design), or JSON for clients that ask only for JSON.
GET /robots.txt, /sitemap.xml — search engines and AI search may crawl, AI training is asked not to; the page is "/".
GET /archive, /retro — old URLs (the removed classic page, an evaluation URL); redirect to /.
GET /static/<file> — the page's stylesheet and script (StaticAssets, mounted in main.py).
"""

import hashlib
import secrets
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from config import ALL_VIEW, CATALOG, PAYWALLED_HOSTS, settings
from deps import limiter
from services.geo import region_for_country

router = APIRouter()

templates = Jinja2Templates(directory="templates")

# Region and topic tabs for the main page, in display order
_VIEWS = [{"id": ALL_VIEW, "label": "All"}] + [{"id": r.id, "label": r.label} for r in CATALOG.regions]
_TOPICS = [{"id": "everything", "label": "Everything"}] + [{"id": t.id, "label": t.label} for t in CATALOG.topics]

# The page links to its stylesheet and script by a hash of their content, so browsers and the CDN
# can keep them for a year: a changed file gets a new URL
_ASSET_HASHES = {name: hashlib.sha256(Path("static", name).read_bytes()).hexdigest()[:12] for name in ("retro.css", "retro.js")}
_ASSETS = {name: f"/static/{name}?v={digest}" for name, digest in _ASSET_HASHES.items()}


class StaticAssets(StaticFiles):
    """static/: kept for a year when asked for by its current hash (_ASSETS), otherwise revalidated each time."""

    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        current = _ASSET_HASHES.get(Path(full_path).name)
        versioned = current is not None and scope["query_string"] == f"v={current}".encode()
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable" if versioned else "no-cache"
        return response


# The page's own script and stylesheet carry a per-response nonce; nothing else may run.
# Images: our own (icons, site icons); connections: our own API.
_CSP = (
    "default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; img-src 'self'; "
    "connect-src 'self'; manifest-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


@router.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
@limiter.limit(settings.API_RATE_LIMIT)
async def root(request: Request):
    accept_header = request.headers.get("accept", "")
    # The page, unless JSON is all the client asks for (crawlers and tools often send */* or nothing)
    if "text/html" in accept_header or "application/json" not in accept_header or request.method == "HEAD":
        nonce = secrets.token_urlsafe(16)
        response = templates.TemplateResponse(
            request, "retro.html", {"views": _VIEWS, "topics": _TOPICS, "paywalled": PAYWALLED_HOSTS, "csp_nonce": nonce,
             "assets": _ASSETS, "site_url": _site_url(request),
             # Starting tab for visitors who haven't chosen one (country from Cloudflare)
             "home_region": region_for_country(request.headers.get("cf-ipcountry"))},
        )
        response.headers["Content-Security-Policy"] = _CSP.format(nonce=nonce)
        response.headers["Vary"] = "Accept"  # The same address answers JSON too: caches must keep both apart
        return response
    return JSONResponse({
        "message": "News Aggregator API is running.",
        "version": settings.APP_VERSION,
        "docs": "/docs" if settings.ENABLE_API_DOCS else "disabled",
    }, headers={"Vary": "Accept"})


def _site_url(request: Request) -> str:
    """The site's address as visitors see it (the public site is HTTPS only; the Host header is checked in main.py)."""
    return f"https://{request.url.netloc}/"


# Search engines and AI assistants' search and on-demand readers may crawl (they link back);
# crawlers that only collect training data are asked not to. Content-Signal: Cloudflare's convention.
_ROBOTS = """User-agent: *
Content-Signal: search=yes, ai-train=no
Allow: /

User-agent: GPTBot
User-agent: ClaudeBot
User-agent: CCBot
User-agent: Google-Extended
User-agent: Applebot-Extended
User-agent: meta-externalagent
Disallow: /

Sitemap: {site_url}sitemap.xml
"""


@router.get("/robots.txt", include_in_schema=False)
async def robots(request: Request):
    return PlainTextResponse(_ROBOTS.format(site_url=_site_url(request)),
                             headers={"Cache-Control": "public, max-age=86400"})


@router.get("/sitemap.xml", include_in_schema=False)
async def sitemap(request: Request):
    return Response(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f'  <url><loc>{_site_url(request)}</loc></url>\n'
        '</urlset>\n',
        media_type="application/xml", headers={"Cache-Control": "public, max-age=86400"})


@router.get("/archive", include_in_schema=False)
@router.get("/retro", include_in_schema=False)
async def old_url_redirect():
    return RedirectResponse("/", status_code=301)
