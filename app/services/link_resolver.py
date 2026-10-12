"""
Google News link resolution: custom resolver first, independent fallback second.

The custom resolver (services/redirect.py) always runs first, and its result is
used whenever it found the publisher's URL. The fallback runs only when the
custom resolver really failed, i.e. its result is still a news.google.com URL.

The fallback is deliberately independent of the custom code path: instead of
the `c-wiz[data-p]` payload, it reads the article's signature and timestamp
(`data-n-a-sg` / `data-n-a-ts`) from the article page and builds its own
batchexecute request and response parsing. If Google changes the page in a way
that breaks one method, the other may still work.
"""

import asyncio
import json
import re
from typing import Optional
from urllib.parse import quote

import httpx
import redis.asyncio as aioredis
import structlog
from bs4 import BeautifulSoup

from config import settings
from metrics import REDIRECT_FALLBACKS
from services.redirect import RESOLVED_URL_PREFIX, RESOLVED_URL_TTL, resolve_redirect

logger = structlog.get_logger(__name__)

_ARTICLE_ID = re.compile(r"news\.google\.com/(?:rss/)?articles/([^/?#]+)")
_BATCH_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
)

# The fallback only runs for links the custom resolver failed on; if that's
# every link (Google changed something), don't fire them all at once.
_FALLBACK_SEMAPHORE = asyncio.Semaphore(5)


def _is_unresolved(url: str) -> bool:
    return "news.google.com" in url


async def decode_with_signature(client: httpx.AsyncClient, url: str) -> Optional[str]:
    """Decodes a Google News article URL via its signature/timestamp. Returns None if it can't."""
    match = _ARTICLE_ID.search(url)
    if not match:
        return None
    article_id = match.group(1)

    async with _FALLBACK_SEMAPHORE:
        page = await client.get(
            f"https://news.google.com/rss/articles/{article_id}",
            headers={"user-agent": _USER_AGENT},
            follow_redirects=True,
            timeout=settings.REQUEST_TIMEOUT,
        )
        page.raise_for_status()
        element = BeautifulSoup(page.text, "html.parser").select_one("c-wiz > div[jscontroller]")
        signature = element.get("data-n-a-sg") if element else None
        timestamp = element.get("data-n-a-ts") if element else None
        if not signature or not timestamp:
            return None

        inner = (
            '["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
            f'"X","X",1,[1,1,1],1,1,null,0,0,null,0],"{article_id}",{timestamp},"{signature}"]'
        )
        response = await client.post(
            _BATCH_URL,
            headers={"content-type": "application/x-www-form-urlencoded;charset=UTF-8", "user-agent": _USER_AGENT},
            content=f"f.req={quote(json.dumps([[['Fbv4je', inner]]]))}",
            timeout=settings.REQUEST_TIMEOUT,
        )
        response.raise_for_status()

    try:
        # Body is ")]}'\n\n<json>"; the first entry's third element is a JSON string ["garturlres", "<url>", ...]
        decoded = json.loads(json.loads(response.text.split("\n\n")[1])[0][2])[1]
    except (IndexError, KeyError, TypeError, ValueError):
        return None
    if isinstance(decoded, str) and decoded.startswith("http") and not _is_unresolved(decoded):
        return decoded
    return None


async def resolve_link(client: httpx.AsyncClient, redis_client: aioredis.Redis, url: str) -> str:
    """Resolves a headline link: custom resolver first, fallback only if it failed."""
    if not _is_unresolved(url):
        return url  # Already a publisher link (e.g. Hacker News); nothing to resolve
    resolved = await resolve_redirect(client, redis_client, url)
    if not _is_unresolved(resolved):
        return resolved

    try:
        fallback = await decode_with_signature(client, url)
    except Exception as e:
        logger.warning("Fallback link resolution errored", url=url, error=str(e))
        fallback = None

    if fallback is None:
        REDIRECT_FALLBACKS.labels(outcome="failed").inc()
        logger.warning("Custom and fallback link resolution both failed", url=url)
        return resolved

    REDIRECT_FALLBACKS.labels(outcome="success").inc()
    # Debug, not info: if the custom resolver breaks, this fires for every link every
    # refresh. Watch redirect_fallbacks_total instead.
    logger.debug("Resolved link via fallback", url=url, resolved=fallback)
    try:
        # Overwrites anything the custom resolver cached (e.g. a Google landing URL)
        await redis_client.set(f"{RESOLVED_URL_PREFIX}{url}", fallback, ex=RESOLVED_URL_TTL)
    except Exception as e:
        logger.debug("URL cache write failed (non-critical)", url=url, error=str(e))
    return fallback
