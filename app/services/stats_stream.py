"""
Every fetched headline, for the stats service (stats/): each new link is added
once to a capped Redis stream, whether or not any tab shows it. A failure here
is logged and never touches a refresh; the stats service drops repeats, so a
restart here, which sends every link again, does no harm.
"""

import asyncio
from collections import OrderedDict
from typing import Dict, List

import redis.asyncio as aioredis
import structlog

from catalog import get_catalog
from config import settings

logger = structlog.get_logger(__name__)

STREAM = "stats:items"
MAX_SENT_LINKS = 200_000

_sent: "OrderedDict[str, None]" = OrderedDict()  # Links already in the stream, oldest first


def _labels() -> Dict[str, str]:
    """Each feed's label, by feed name."""
    catalog = get_catalog()
    feeds = [s for region in catalog.regions for s in region.sources + region.topic_sources]
    return {s.name: s.label or "" for s in feeds + catalog.extras + catalog.topic_extras}


async def _send(redis_client: aioredis.Redis, results_by_source: Dict[str, List[Dict]]) -> None:
    labels = _labels()
    new = {
        item["link"]: (item, labels.get(name, ""))
        for name, items in results_by_source.items() for item in items if item["link"] not in _sent
    }
    if not new:
        return
    pipe = redis_client.pipeline(transaction=False)
    for item, label in new.values():
        published = item.get("published")
        pipe.xadd(
            STREAM,
            {"title": item["title"], "link": item["link"], "source": item["source"],
             "published": "" if published is None else str(published), "label": label},
            maxlen=settings.STATS_STREAM_MAXLEN, approximate=True,
        )
    await pipe.execute()
    _sent.update(dict.fromkeys(new))
    while len(_sent) > MAX_SENT_LINKS:
        _sent.popitem(last=False)


async def publish_new(redis_client: aioredis.Redis, results_by_source: Dict[str, List[Dict]]) -> None:
    """Adds the links not yet sent to the stream. Never raises."""
    if not settings.STATS_STREAM_ENABLED:
        return
    try:
        await asyncio.wait_for(_send(redis_client, results_by_source), settings.STATS_STREAM_TIMEOUT_SECONDS)
    except Exception as e:
        logger.warning("Stats stream not written", error=str(e) or type(e).__name__)
