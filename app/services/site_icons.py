"""
Website icons ("site icons" on the page): one small icon per publisher domain,
fetched by the refresher from an icon service and cached in Redis, then served
by the app from our own domain, so visitors' browsers never contact the
publishers or the icon service.

For a website the icon service has none for, the refresher finds the
website's own icon (the largest one its homepage declares, or /favicon.ico;
Google's copy when those are missing or tiny) and stores it redrawn as
pixel art (services/pixel_icons.py).
"""

import asyncio
import base64
import json
import re
from typing import Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import httpx
import redis.asyncio as aioredis
import structlog
from bs4 import BeautifulSoup

from config import settings
from services.pixel_icons import picture_size, pixel_art
from services.safe_fetch import UnsafeURL, stream_public

logger = structlog.get_logger(__name__)

_HOST = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
_SEMAPHORE = asyncio.Semaphore(10)
MAX_ICON_BYTES = 64_000
PAGE_HEADERS = {"User-Agent": settings.BROWSER_USER_AGENT, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"}
IMAGE_HEADERS = {"User-Agent": settings.BROWSER_USER_AGENT, "Accept": "image/*,*/*;q=0.5"}
MAX_PAGE_BYTES = 512_000  # Only the start of a homepage is read: icons are declared in its <head>
MAX_SOURCE_BYTES = 1_000_000
MAX_ICON_LINKS = 6
GOOD_SIZE = 32  # A smaller own icon gives way to Google's copy (64 px)


def icon_host(link: str) -> Optional[str]:
    """The domain an item's icon is stored under ("www." dropped, as on the page), or None."""
    host = (urlparse(link).hostname or "").lower().removeprefix("www.")
    return host if _HOST.match(host) else None


def icon_key(host: str) -> str:
    return f"icon:{host}"


async def _download(client: httpx.AsyncClient, url: str, headers: dict, limit: int) -> Optional[Tuple[str, bytes]]:
    """(final URL, body) of a URL answering 200, public addresses only; the body is cut at `limit` bytes."""
    try:
        async with stream_public(client, url, headers) as response:
            if response.status_code != 200:
                return None
            body = b""
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) >= limit:
                    break
            return str(response.url), body[:limit]
    except (httpx.HTTPError, UnsafeURL):
        return None


async def _own_icons(client: httpx.AsyncClient, host: str) -> List[bytes]:
    """The raster icons the website's homepage declares, and its /favicon.ico."""
    urls = []
    for home in (f"https://{host}/", f"https://www.{host}/"):
        page = await _download(client, home, PAGE_HEADERS, MAX_PAGE_BYTES)
        if page:
            base, body = page
            for link in BeautifulSoup(body, "html.parser").find_all("link", href=True):
                rel, href = " ".join(link.get("rel") or []).lower(), link["href"].strip()
                if "icon" in rel and "mask-icon" not in rel and not href.startswith("data:") \
                        and "svg" not in (link.get("type") or "") and not href.split("?")[0].endswith(".svg"):
                    urls.append(urljoin(base, href))
            break
    urls.append(f"https://{host}/favicon.ico")
    icons = []
    for url in list(dict.fromkeys(urls))[:MAX_ICON_LINKS]:
        icon = await _download(client, url, IMAGE_HEADERS, MAX_SOURCE_BYTES)
        if icon:
            icons.append(icon[1])
    return icons


async def _pixel_icon(client: httpx.AsyncClient, host: str) -> Optional[Tuple[bytes, str]]:
    """(pixel-art SVG, where its source came from) for a website, or None if no icon was found."""
    sized = [(picture_size(data), data) for data in await _own_icons(client, host)]
    own = max(((size, data) for size, data in sized if size), key=lambda pair: pair[0], default=None)
    if own and own[0] >= GOOD_SIZE:
        svg = await asyncio.to_thread(pixel_art, own[1])
        if svg:
            return svg, "own"
    if settings.ICON_FALLBACK_URL:  # Google's copy, only when its own is missing, small or unusable
        google = await _download(client, settings.ICON_FALLBACK_URL.format(host=host), IMAGE_HEADERS, MAX_SOURCE_BYTES)
        svg = google and await asyncio.to_thread(pixel_art, google[1])
        if svg:
            return svg, "google"
    if own and own[0] < GOOD_SIZE:
        svg = await asyncio.to_thread(pixel_art, own[1])
        if svg:
            return svg, "own"
    return None


async def _fetch(client: httpx.AsyncClient, redis_client: aioredis.Redis, host: str) -> Optional[str]:
    """Stores the website's icon: "service", "own" or "google" (where it came from), or None if it has none."""
    try:
        async with _SEMAPHORE:
            response = await client.get(settings.ICON_SOURCE_URL.format(host=host), follow_redirects=True)
        content_type = response.headers.get("content-type", "").split(";")[0]
        usable = response.status_code == 200 and content_type.startswith("image/") and 0 < len(response.content) <= MAX_ICON_BYTES
    except httpx.HTTPError:
        usable = False
    if usable:
        data, via = response.content, "service"
    else:
        try:
            async with _SEMAPHORE:
                pixel = await _pixel_icon(client, host)
        except Exception as e:  # Costs only this icon
            logger.warning("Site icon not redrawn", host=host, error=str(e) or type(e).__name__)
            pixel = None
        if pixel is None:  # Marked, so it isn't retried every cycle
            await redis_client.set(icon_key(host), "", ex=settings.ICON_FAILED_TTL_SECONDS)
            return None
        (data, via), content_type = pixel, "image/svg+xml"
    value = json.dumps({"type": content_type, "data": base64.b64encode(data).decode(), "via": via})
    await redis_client.set(icon_key(host), value, ex=settings.ICON_TTL_SECONDS)
    return via


async def cache_icons(client: httpx.AsyncClient, redis_client: aioredis.Redis, links: Iterable[str]) -> int:
    """Fetches the icons of the links' domains that aren't cached (or marked) yet. Returns how many were stored."""
    hosts = sorted({host for host in map(icon_host, links) if host})
    if not hosts:
        return 0
    known = await redis_client.mget([icon_key(host) for host in hosts])
    missing = [host for host, value in zip(hosts, known) if value is None]
    found = await asyncio.gather(*(_fetch(client, redis_client, host) for host in missing))
    stored = sum(1 for via in found if via)
    if missing:
        logger.info("Cached site icons", stored=stored, pixel_art=sum(1 for via in found if via in ("own", "google")),
                    unavailable=len(missing) - stored)
    return stored


async def get_icon(redis_client: aioredis.Redis, host: str) -> Optional[tuple[str, bytes]]:
    """(content type, bytes) of a cached icon, or None."""
    if not _HOST.match(host):
        return None
    value = await redis_client.get(icon_key(host))
    if not value:
        return None
    icon = json.loads(value)
    return icon["type"], base64.b64decode(icon["data"])
