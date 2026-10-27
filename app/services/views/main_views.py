"""
The main views: "all" and each region's, every one with the same mix of the
region's news and the extras (build_views).
"""

import time
from typing import Dict, List

import httpx
import redis.asyncio as aioredis
import structlog

from catalog import ALL_VIEW, Catalog, NewsSource, get_catalog
from services.feeds.poller import FeedPoller
from services.links import resolve_links
from services.views.mixing import build_limit, mix_headlines

logger = structlog.get_logger(__name__)


def build_views(catalog: Catalog, results_by_source: Dict[str, List[Dict]], now: float) -> Dict[str, List[Dict]]:
    """
    Builds every view with the same mix: news (that region's, or all regions
    equally for "all") plus the extras (science, Hacker News) at their weights.

    1. News lists: each region from its own sources, and "world" from the
       regional lists, equally (a story two regions both carry appears once;
       the publisher cap applies across regions).
    2. Extras lists: each extra on its own, publisher-capped.
    3. Each view interleaves its news list with the extras lists. Everything is
       already capped, so this step only interleaves and de-duplicates. News has
       the largest single weight, so it leads every view.

    A view whose news list is empty (all its feeds failed) stays empty, so its
    previous snapshot is kept rather than replaced by extras alone.
    """
    regional = {
        region.id: mix_headlines(region.sources, results_by_source, now, view=f"{region.id}/news", limit=build_limit())
        for region in catalog.regions
    }
    world = mix_headlines(
        [NewsSource(name=f"region:{region_id}", url="", weight=1 / len(regional)) for region_id in regional],
        {f"region:{region_id}": items for region_id, items in regional.items()},
        now,
        view=f"{ALL_VIEW}/news",
        limit=build_limit(),
    )
    extras = {source.name: mix_headlines([source], results_by_source, now, view=source.name, limit=build_limit())
              for source in catalog.extras}
    news_weight = 1 - sum(source.weight for source in catalog.extras)

    def with_extras(view: str, news: List[Dict]) -> List[Dict]:
        if not news:
            return []
        sources = [NewsSource(name="news", url="", weight=news_weight)] + catalog.extras
        return mix_headlines(sources, {"news": news} | extras, now, view=view, cap_publishers=False, limit=build_limit())

    views = {region_id: with_extras(region_id, items) for region_id, items in regional.items()}
    views[ALL_VIEW] = with_extras(ALL_VIEW, world)
    return views


async def get_fresh_views(
    client: httpx.AsyncClient,
    redis_client: aioredis.Redis,
    poller: FeedPoller,
) -> tuple[Dict[str, List[Dict]], Dict[str, List[Dict]]]:
    """
    Fetches every feed in the catalog once, builds each region's list and the
    "all" list (see build_views), and resolves links.
    Returns ({view_id: headlines}, {source name: fetched headlines}).
    """
    catalog = get_catalog()
    sources = [s for region in catalog.regions for s in region.sources] + catalog.extras
    results_by_source = await poller.fetch_all(client, sources)

    views = build_views(catalog, results_by_source, time.time())
    if not any(views.values()):
        logger.error("Failed to fetch any headlines from any source")
        return views, results_by_source

    # The same headline object can appear in a region and in "all"; resolve each once
    unique = list({id(h): h for items in views.values() for h in items}.values())
    await resolve_links(client, redis_client, unique)
    return views, results_by_source
