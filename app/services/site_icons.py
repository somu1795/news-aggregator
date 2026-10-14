"""
Website icons ("site icons" on the page): one small icon per publisher domain,
fetched by the refresher from an icon service and cached in Redis, then served
by the app from our own domain, so visitors' browsers never contact the
publishers or the icon service. A website without one gets a pixel-art tile
with its initial instead (services/pixel_icons.py).
"""

import asyncio
import base64
import json
import re
from typing import Iterable, Optional
from urllib.parse import urlparse

import httpx
import redis.asyncio as aioredis
import structlog

from config import settings
from services.pixel_icons import pixel_icon

logger = structlog.get_logger(__name__)

_HOST = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
_SEMAPHORE = asyncio.Semaphore(10)
MAX_ICON_BYTES = 64_000


def icon_host(link: str) -> Optional[str]:
    """The domain an item's icon is stored under ("www." dropped, as on the page), or None."""
    host = (urlparse(link).hostname or "").lower().removeprefix("www.")
    return host if _HOST.match(host) else None


def icon_key(host: str) -> str:
    return f"icon:{host}"


async def _fetch(client: httpx.AsyncClient, redis_client: aioredis.Redis, host: str) -> bool:
    try:
        async with _SEMAPHORE:
            response = await client.get(settings.ICON_SOURCE_URL.format(host=host), follow_redirects=True)
        content_type = response.headers.get("content-type", "").split(";")[0]
        usable = response.status_code == 200 and content_type.startswith("image/") and 0 < len(response.content) <= MAX_ICON_BYTES
    except httpx.HTTPError:
        usable = False
    if not usable:  # Marked, so it isn't retried every cycle
        await redis_client.set(icon_key(host), "", ex=settings.ICON_FAILED_TTL_SECONDS)
        return False
    value = json.dumps({"type": content_type, "data": base64.b64encode(response.content).decode()})
    await redis_client.set(icon_key(host), value, ex=settings.ICON_TTL_SECONDS)
    return True


async def cache_icons(client: httpx.AsyncClient, redis_client: aioredis.Redis, links: Iterable[str]) -> int:
    """Fetches the icons of the links' domains that aren't cached (or marked) yet. Returns how many were stored."""
    hosts = sorted({host for host in map(icon_host, links) if host})
    if not hosts:
        return 0
    known = await redis_client.mget([icon_key(host) for host in hosts])
    missing = [host for host, value in zip(hosts, known) if value is None]
    stored = sum(await asyncio.gather(*(_fetch(client, redis_client, host) for host in missing)))
    if missing:
        logger.info("Cached site icons", stored=stored, unavailable=len(missing) - stored)
    return stored


async def get_icon(redis_client: aioredis.Redis, host: str) -> Optional[tuple[str, bytes, bool]]:
    """
    (content type, bytes, generated) of a website's icon: the cached one, or a pixel-art
    tile if it has none. None if the refresher hasn't looked for it yet.
    """
    if not _HOST.match(host):
        return None
    value = await redis_client.get(icon_key(host))
    if value is None:
        return None
    if value == "":  # Marked: the icon service had none
        return "image/svg+xml", pixel_icon(host), True
    icon = json.loads(value)
    return icon["type"], base64.b64decode(icon["data"]), False
