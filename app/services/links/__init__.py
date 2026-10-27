"""
Headline links: a feed's link can point at an aggregator (Google News) rather
than the publisher. resolve_links() replaces each with the publisher's own;
links that already are the publisher's are left as they are.
"""

import asyncio
from typing import Dict, List

import httpx
import redis.asyncio as aioredis
import structlog

from services.links.google_news import resolve_link

logger = structlog.get_logger(__name__)

# Link resolution talks to Google; a refresh can have ~200 new links (first run),
# so bound it instead of firing them all at once.
_RESOLVE_SEMAPHORE = asyncio.Semaphore(10)


async def resolve_links(client: httpx.AsyncClient, redis_client: aioredis.Redis, headlines: List[Dict]) -> None:
    """Resolves each distinct link once and updates every headline that uses it."""
    links = sorted({h["link"] for h in headlines})

    async def resolve(link: str) -> str:
        async with _RESOLVE_SEMAPHORE:
            return await resolve_link(client, redis_client, link)

    # The custom resolver runs first; a fallback runs only for links it failed on.
    results = await asyncio.gather(*(resolve(link) for link in links), return_exceptions=True)
    resolved = {}
    for link, result in zip(links, results):
        if isinstance(result, Exception):
            logger.warning("Failed to resolve redirect", link=link, error=str(result))
        else:
            resolved[link] = result
    for headline in headlines:
        headline["link"] = resolved.get(headline["link"], headline["link"])
