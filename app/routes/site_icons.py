"""
GET /site-icon/<domain> — a publisher's icon, if the refresher has cached it
(services/site_icons.py). Only domains in published headlines are ever
cached, so this never fetches anything.
"""

from fastapi import APIRouter, Request, Response

from services.site_icons import get_icon

router = APIRouter()


@router.api_route("/site-icon/{host}", methods=["GET", "HEAD"], include_in_schema=False)
async def site_icon(request: Request, host: str):
    try:
        icon = await get_icon(request.app.state.redis, host)
    except Exception:  # Redis down: the page just shows no icon
        icon = None
    if icon is None:
        # Briefly: a new item's icon is usually cached within the same refresh cycle
        return Response(status_code=404, headers={"Cache-Control": "public, max-age=300"})
    content_type, data = icon
    # Opened directly, an icon is a page on our domain, and an SVG one (a publisher's, via the icon
    # service) could carry a script: nothing in it may run or load. As an <img> on the page, it can't anyway.
    return Response(data, media_type=content_type, headers={"Cache-Control": "public, max-age=604800",
                                                            "Content-Security-Policy": "default-src 'none'; sandbox"})
