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
        return Response(status_code=404, headers={"Cache-Control": "public, max-age=3600"})
    content_type, data = icon
    return Response(data, media_type=content_type, headers={"Cache-Control": "public, max-age=604800"})
