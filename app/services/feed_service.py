"""
RSS feed fetching, parsing, and weighted headline aggregation.

Uses a concurrency semaphore instead of a rate limiter to bound outgoing
requests without artificially throttling throughput.
"""

import asyncio
import logging
import random
import time
from typing import Dict, List

import feedparser
import httpx
import redis.asyncio as aioredis

from config import settings
from services.redirect import resolve_redirect

logger = logging.getLogger(__name__)

# Concurrency semaphore — limits parallel outgoing HTTP requests.
# Much more efficient than the previous 20-req/min rate limiter which
# guaranteed 90s+ latency on cache misses with 30+ headlines to resolve.
_OUTGOING_SEMAPHORE = asyncio.Semaphore(15)

_FEED_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/xml,application/xhtml+xml,text/html;q=0.9, text/plain;q=0.8,*/*;q=0.5",
    "Accept-Language": "en-US,en;q=0.5",
}


async def fetch_and_parse_feed(
    client: httpx.AsyncClient,
    source_name: str,
    url: str,
) -> List[Dict]:
    """Fetches and parses a single RSS feed with retry logic."""
    for attempt in range(settings.FETCH_RETRIES):
        try:
            async with _OUTGOING_SEMAPHORE:
                response = await client.get(url, headers=_FEED_HEADERS)
            response.raise_for_status()
            feed = feedparser.parse(response.content)
            if getattr(feed, "bozo", False):
                raise ValueError(f"Feed parse error for {source_name}: {feed.bozo_exception}")

            headlines = []
            for entry in feed.entries:
                headlines.append({
                    "title": entry.title,
                    "link": entry.link,
                    # Prioritize the source from the article entry, fallback to the feed's name from config
                    "source": getattr(entry, "source", {}).get("title") or source_name,
                    "published": (
                        time.mktime(entry.published_parsed)
                        if getattr(entry, "published_parsed", None)
                        else time.time()
                    ),
                    "last_updated": time.time(),
                })
            logger.info(f"Fetched {len(headlines)} headlines from {source_name} at {url}")
            return headlines
        except Exception as e:
            logger.warning(f"Attempt {attempt + 1} failed for {source_name} at {url}: {e}")
            if attempt < settings.FETCH_RETRIES - 1:
                await asyncio.sleep(settings.BACKOFF_BASE ** attempt)
            else:
                logger.error(
                    f"Failed to fetch from {source_name} at {url} after {settings.FETCH_RETRIES} attempts.",
                    exc_info=True,
                )
                return []
    return []


async def get_fresh_headlines(
    client: httpx.AsyncClient,
    redis_client: aioredis.Redis,
) -> List[Dict]:
    """
    Fetches headlines from all configured news sources concurrently, applies
    weighted allocation, shuffles the results, and resolves redirect URLs.
    """
    # 1. Fetch from all sources concurrently
    fetch_tasks = [
        fetch_and_parse_feed(client, name, url)
        for name, url in settings.NEWS_SOURCES.items()
    ]
    source_names = list(settings.NEWS_SOURCES.keys())
    results_by_source_list = await asyncio.gather(*fetch_tasks)
    results_by_source = dict(zip(source_names, results_by_source_list))

    # 2. Sort each source's headlines by date
    for name in results_by_source:
        results_by_source[name].sort(key=lambda x: x["published"], reverse=True)

    # 3. Allocate headlines based on weights from config
    allocations = {
        name: round(settings.MAX_HEADLINES * weight)
        for name, weight in settings.SOURCE_WEIGHTS.items()
    }

    # Adjust for rounding errors to ensure the total is exactly MAX_HEADLINES
    total_allocated = sum(allocations.values())
    if total_allocated != settings.MAX_HEADLINES:
        remainder = settings.MAX_HEADLINES - total_allocated
        primary_source = max(settings.SOURCE_WEIGHTS, key=settings.SOURCE_WEIGHTS.get)
        if primary_source in allocations:
            allocations[primary_source] += remainder

    # 4. Gather headlines according to the calculated allocations
    weighted_headlines = []
    for source_name, num_to_take in allocations.items():
        source_headlines = results_by_source.get(source_name, [])
        weighted_headlines.extend(source_headlines[:num_to_take])

    # 5. Shuffle the final combined list for a random mix
    random.shuffle(weighted_headlines)

    if not weighted_headlines:
        logger.error("Failed to fetch any headlines from any source.")
        return []

    logger.info(
        f"Aggregated {len(weighted_headlines)} headlines from "
        f"{len(settings.NEWS_SOURCES)} sources with weighting."
    )

    # 6. Resolve redirects for the final list of headlines (with URL caching)
    resolve_tasks = [
        resolve_redirect(client, redis_client, h["link"])
        for h in weighted_headlines
    ]
    resolved_links = await asyncio.gather(*resolve_tasks)
    for i, headline in enumerate(weighted_headlines):
        headline["link"] = resolved_links[i]

    return weighted_headlines
