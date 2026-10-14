"""
Background headline refresher.

Runs as its own container (a single replica). Every REFRESH_INTERVAL_SECONDS
it fetches all feeds, resolves links, and publishes one snapshot per view
("all" and each region) to Redis at snapshot_key(view). Then, as a second
phase, it has the tagger tag every headline and publishes one snapshot per
region (and "all") per topic. The API only ever reads those snapshots, so
user traffic never triggers upstream fetches. Items carry their article
summary when the summarizer (summarizer.py) has made one. Every published
item is also kept in the archive (services/archive.py), when ARCHIVE_PATH is set.

If a refresh fails, or a view comes back empty, that view's previous snapshot
is left in place: users keep getting slightly older headlines instead of errors.
If the topic phase fails (e.g. the tagger is down), the main views are already
published and the topic views keep their previous snapshots.
"""

import asyncio
import json
import time
from typing import Dict, List

import httpx
import redis.asyncio as aioredis
import structlog
from prometheus_client import Counter, Gauge, Histogram, start_http_server

from config import CATALOG, feed_text_key, settings, snapshot_key, summary_key
from logging_config import configure_logging
from metrics import REFRESHES
from models import APIResponse
from services.archive import archive
from services.feed_service import get_fresh_views, get_topic_views
from services.redis_client import create_redis_client
from services.site_icons import cache_icons
from services.story_groups import group_stories

logger = structlog.get_logger(__name__)

# Declared here rather than in metrics.py: unlabeled metrics create a series on
# import, and the API (which imports metrics.py) must not report them as zeros.
REFRESH_DURATION = Histogram(
    "headlines_refresh_duration_seconds",
    "Time taken by one refresh cycle",
    buckets=(1, 2, 5, 10, 20, 30, 60, 120),
)
LAST_REFRESH_SUCCESS = Gauge(
    "headlines_last_refresh_success_timestamp_seconds",
    "Unix time of the last successful refresh",
)
HEADLINES_FETCHED = Gauge(
    "headlines_fetched_count",
    "Number of headlines in the last published snapshot, per view",
    ["region"],
)
TOPIC_REFRESHES = Counter(
    "headlines_topic_refreshes_total",
    "Topic view refreshes (the second phase of a refresh cycle), by result",
    ["result"],
)


async def add_summaries(redis_client: aioredis.Redis, views: Dict[str, List[Dict]]) -> None:
    """Sets each item's "summary" from the summarizer's store (summarizer.py), when it has one."""
    items = [item for view_items in views.values() for item in view_items]
    if not items:
        return
    for item, raw in zip(items, await redis_client.mget([summary_key(item["link"]) for item in items])):
        item["summary"] = raw and json.loads(raw).get("text")


async def store_feed_texts(redis_client: aioredis.Redis, views: Dict[str, List[Dict]]) -> None:
    """Keeps the feed's own text of each published item that has enough of it, for the summarizer to fall back on."""
    texts = {item["link"]: item["feed_text"] for items in views.values() for item in items
             if len(item.get("feed_text") or "") >= settings.SUMMARY_MIN_TEXT_CHARS}
    if not texts:
        return
    async with redis_client.pipeline(transaction=False) as pipe:
        for link, text in texts.items():
            pipe.set(feed_text_key(link), text, ex=settings.FEED_TEXT_TTL_SECONDS, nx=True)  # Kept from first sight
        await pipe.execute()


async def publish(redis_client: aioredis.Redis, views: Dict[str, List[Dict]], fetched_at: float) -> None:
    """Writes one snapshot per view, with summaries, and archives them. A view id is a region id or "region:topic"."""
    await add_summaries(redis_client, views)
    await store_feed_texts(redis_client, views)
    async with redis_client.pipeline(transaction=False) as pipe:
        for view, items in views.items():
            region, _, topic = view.partition(":")
            snapshot = APIResponse(
                region=region,
                topic=topic or None,
                data=items,
                fetched_at=fetched_at,
                expires_at=fetched_at + settings.REFRESH_INTERVAL_SECONDS,
            )
            pipe.set(snapshot_key(view), snapshot.model_dump_json())
            HEADLINES_FETCHED.labels(region=view).set(len(items))
        await pipe.execute()
    if settings.ARCHIVE_PATH:
        try:
            await asyncio.to_thread(archive, views, fetched_at)
        except Exception as e:  # The archive must never stop the site's refresh
            logger.warning("Headlines not archived", error=str(e) or type(e).__name__)


async def refresh_once(http_client: httpx.AsyncClient, redis_client: aioredis.Redis) -> int:
    """
    Fetches headlines and publishes one snapshot per non-empty view, then the
    topic views. Returns the number of main views published.
    """
    views, results_by_source = await get_fresh_views(http_client, redis_client)
    fetched_at = time.time()
    published = {view: items for view, items in views.items() if items}
    if not published:
        REFRESHES.labels(result="empty").inc()
        logger.error("Refresh produced no headlines, keeping previous snapshots")
        return 0

    published = await group_stories(http_client, published)
    await publish(redis_client, published, fetched_at)

    empty = sorted(set(views) - set(published))
    if empty:
        logger.warning("Some views had no headlines, keeping their previous snapshots", views=empty)
    REFRESHES.labels(result="success").inc()
    LAST_REFRESH_SUCCESS.set(fetched_at)
    logger.info("Published headline snapshots", counts={view: len(items) for view, items in published.items()})
    await refresh_topics(http_client, redis_client, results_by_source)
    await refresh_site_icons(http_client, redis_client)
    return len(published)


async def refresh_topics(
    http_client: httpx.AsyncClient, redis_client: aioredis.Redis, results_by_source: Dict[str, List[Dict]],
) -> int:
    """Publishes every topic view (empty ones too). Returns how many; 0 if it failed and the previous ones were kept."""
    try:
        views = await asyncio.wait_for(
            get_topic_views(http_client, redis_client, results_by_source), settings.TOPIC_REFRESH_TIMEOUT_SECONDS,
        )
        views = await group_stories(http_client, views)
        await publish(redis_client, views, time.time())
    except Exception as e:
        TOPIC_REFRESHES.labels(result="error").inc()
        logger.warning("Topic views not refreshed, keeping their previous snapshots", error=str(e) or type(e).__name__)
        return 0
    TOPIC_REFRESHES.labels(result="success").inc()
    logger.info("Published topic snapshots", counts={view: len(items) for view, items in views.items()})
    return len(views)


async def refresh_site_icons(http_client: httpx.AsyncClient, redis_client: aioredis.Redis) -> None:
    """Caches the icons of every published item's website that hasn't got one yet; failures only cost the icons."""
    try:
        views = CATALOG.view_ids + CATALOG.topic_view_ids
        snapshots = await redis_client.mget([snapshot_key(view) for view in views])
        links = [item["link"] for raw in snapshots if raw for item in json.loads(raw)["data"]]
        await asyncio.wait_for(cache_icons(http_client, redis_client, links), settings.ICON_REFRESH_TIMEOUT_SECONDS)
    except Exception as e:
        logger.warning("Site icons not refreshed", error=str(e) or type(e).__name__)


async def wait_for_next_cycle(redis_client: aioredis.Redis) -> None:
    """Sleeps until the next cycle, waking early if an admin requested a refresh."""
    try:
        if await redis_client.blpop([settings.REFRESH_REQUEST_KEY], timeout=settings.REFRESH_INTERVAL_SECONDS):
            logger.info("Refresh requested by admin")
            await redis_client.delete(settings.REFRESH_REQUEST_KEY)  # Collapse duplicate requests
    except aioredis.RedisError as e:
        logger.warning("Redis unavailable while waiting, sleeping instead", error=str(e))
        await asyncio.sleep(settings.REFRESH_INTERVAL_SECONDS)


async def run() -> None:
    configure_logging(settings.LOG_LEVEL)
    start_http_server(settings.METRICS_PORT)

    # Socket timeout must outlast the BLPOP wait in wait_for_next_cycle()
    redis_client = create_redis_client(socket_timeout=settings.REFRESH_INTERVAL_SECONDS + 10, retries=3)
    limits = httpx.Limits(max_connections=50, max_keepalive_connections=20)
    async with httpx.AsyncClient(http2=True, timeout=settings.REQUEST_TIMEOUT, limits=limits) as http_client:
        logger.info("Refresher started", interval=settings.REFRESH_INTERVAL_SECONDS)
        while True:
            started = time.monotonic()
            try:
                await asyncio.wait_for(
                    refresh_once(http_client, redis_client), settings.REFRESH_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                REFRESHES.labels(result="timeout").inc()
                logger.error("Refresh timed out, keeping previous snapshot", timeout=settings.REFRESH_TIMEOUT_SECONDS)
            except Exception as e:
                REFRESHES.labels(result="error").inc()
                logger.error("Refresh failed, keeping previous snapshot", error=str(e), exc_info=True)
            REFRESH_DURATION.observe(time.monotonic() - started)
            await wait_for_next_cycle(redis_client)


if __name__ == "__main__":
    asyncio.run(run())
