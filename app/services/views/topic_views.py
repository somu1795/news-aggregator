"""
The topic views: one per region (and "all") per topic, from the headlines the
tagger tagged with it and the feeds about it (build_topic_views).
"""

import json
import random
import time
from collections import Counter
from typing import Dict, List, Optional, Set, Tuple

import httpx
import redis.asyncio as aioredis
import structlog

from catalog import ALL_VIEW, OTHER_LABEL, Catalog, NewsSource, Topic, get_catalog, topic_view
from config import settings
from keys import topic_check_key, topic_checks_pending_key
from services.feeds.poller import FeedPoller
from services.links import resolve_links
from services.places import matcher_for
from services.views.mixing import build_limit, mix_headlines, title_key

logger = structlog.get_logger(__name__)


async def tag(
    client: httpx.AsyncClient, redis_client: aioredis.Redis, catalog: Catalog, results_by_source: Dict[str, List[Dict]],
) -> Tuple[Dict[str, Set[str]], Set[str]]:
    """
    Tags every fetched headline: the tagger's topics, plus the topic its feed is
    about (a Sports section's items are always "sport"), unless the topic has a
    min_feed_score and the tagger's model gives the item less than that for it
    (its feed may have misfiled it) and the summarizer's LLM hasn't said yes to
    the topic's question about it (topic_check_key). Items not judged yet are
    listed for the summarizer (topic_checks_pending_key). The feed's label is
    also sent to the tagger, which stores it as training data.
    Returns ({title: topics}, titles left out of their feed's topic).
    """
    labels = {s.name: s.label for s in catalog.labelled_sources}
    candidates: Dict[str, tuple] = {}  # title -> (headline, label); a topic label beats "other" or none
    for name, items in results_by_source.items():
        label = labels.get(name)
        for headline in items:
            previous = candidates.get(headline["title"])
            if previous is None or (previous[1] in (None, OTHER_LABEL) and label not in (None, OTHER_LABEL)):
                candidates[headline["title"]] = (headline, label)
    payload = [
        {"title": h["title"], "source": h["source"], "link": h["link"], "label": label}
        for h, label in candidates.values()
    ]
    response = await client.post(f"{settings.TAGGER_URL}/tag", json={"items": payload}, timeout=settings.TAGGER_TIMEOUT)
    response.raise_for_status()
    body = response.json()
    minimum = {topic.id: topic.min_feed_score for topic in catalog.topics if topic.min_feed_score is not None}
    scores = body.get("scores") or [None] * len(payload)  # A tagger from before scores: every item stays
    doubtful = [(item, score) for item, score in zip(payload, scores)
                if item["label"] in minimum and score is not None and score < minimum[item["label"]]]
    verdicts = await redis_client.mget([topic_check_key(i["label"], i["title"]) for i, _ in doubtful]) if doubtful else []
    rescued = {i["title"] for (i, _), v in zip(doubtful, verdicts) if v and json.loads(v)["yes"]}
    misfiled = {i["title"] for i, _ in doubtful} - rescued
    await redis_client.set(topic_checks_pending_key(), json.dumps(
        [{"topic": i["label"], "title": i["title"], "source": i["source"]} for (i, _), v in zip(doubtful, verdicts) if v is None]))
    tags_by_title = {}
    for item, tags in zip(payload, body["tags"]):
        label = None if item["title"] in misfiled else item["label"]
        tags_by_title[item["title"]] = set(tags) | ({label} if label not in (None, OTHER_LABEL) else set())
    logger.info("Tagged headlines", count=len(payload), model=body["model"], misfiled=len(misfiled), rescued=len(rescued))
    return tags_by_title, misfiled


def build_topic_views(
    catalog: Catalog, results_by_source: Dict[str, List[Dict]], tags_by_title: Dict[str, Set[str]], now: float,
    rng: Optional[random.Random] = None,
) -> Dict[str, List[Dict]]:
    """
    Builds one view per region (and "all") per topic, from the items tagged with
    that topic. The news/extras mix of the main views doesn't apply to a single
    topic (Hacker News has little sport). Instead, items come in this order:

    1. The feeds about the topic (their items are tagged by their feed, so
       they're precise), mixed equally. In a region's view: first the stories
       only that region's own feeds have; then the shared feeds' (extras, topic
       extras: worldwide outlets like Mongabay) stories about that region,
       i.e. naming one of its places (regions.yaml, services/places.py); then their stories
       that name no place; then global stories: those in two or more regions'
       own feeds (e.g. a TechCrunch story in several Google News Technology
       editions). A shared feed's story about another region is left out, so
       region views don't all show the same worldwide outlets.
    2. Items the model tagged from other feeds, in the same order, only to
       fill the view, and never for a feed_only topic (topics.yaml).

    `rng`: where each mix starts (mix_headlines). Empty views are kept: a region can have no uplifting news right now.
    """
    shared = catalog.extras + catalog.topic_extras
    item_regions = matcher_for(catalog).item_regions
    own = {region.id: region.topic_sources + region.sources for region in catalog.regions}
    everything = [s for sources in own.values() for s in sources] + shared

    def equal(sources) -> List[NewsSource]:
        return [NewsSource(name=s.name, url=s.url, weight=1.0, max_items=s.max_items) for s in sources]

    def view_for(groups, topic: Topic, view: str) -> List[Dict]:
        """groups: (sources, their tagged items), in order of preference."""
        def mixed(sources, items):
            return mix_headlines(equal(sources), items, now, view=view, max_age_hours=topic.max_age_hours,
                                 limit=build_limit(), rng=rng)
        lists = [mixed([s for s in sources if s.label == topic.id], items) for sources, items in groups]
        if not topic.feed_only:
            lists += [mixed([s for s in sources if s.label != topic.id], items) for sources, items in groups]
        seen, combined = set(), []
        for items in lists:
            for h in items:
                keys = {h["link"], title_key(h)}
                if seen.isdisjoint(keys):
                    seen |= keys
                    combined.append(h)
        return combined[:build_limit()]

    views = {}
    for topic in catalog.topics:
        tagged = {
            name: [h for h in items if topic.id in tags_by_title.get(h["title"], ())]
            for name, items in results_by_source.items()
        }
        regions_of = Counter(
            key for sources in own.values()
            for key in {title_key(h) for s in sources for h in tagged.get(s.name, [])}
        )
        regional = {name: [h for h in items if regions_of[title_key(h)] < 2] for name, items in tagged.items()}
        global_ = {name: [h for h in items if regions_of[title_key(h)] >= 2] for name, items in tagged.items()}
        shared_tagged = {s.name: tagged.get(s.name, []) for s in shared}
        placeless = {name: [h for h in items if not item_regions(h)] for name, items in shared_tagged.items()}
        for region in catalog.regions:
            about = {name: [h for h in items if region.id in item_regions(h)] for name, items in shared_tagged.items()}
            view = topic_view(region.id, topic.id)
            views[view] = view_for(
                [(own[region.id], regional), (shared, about), (shared, placeless), (own[region.id], global_)], topic, view,
            )
        view = topic_view(ALL_VIEW, topic.id)
        views[view] = view_for([(everything, tagged)], topic, view)
    return views


async def get_topic_views(
    client: httpx.AsyncClient,
    redis_client: aioredis.Redis,
    poller: FeedPoller,
    results_by_source: Dict[str, List[Dict]],
) -> Tuple[Dict[str, List[Dict]], List[Dict]]:
    """
    Builds every topic view from this cycle's news (results_by_source, from
    get_fresh_views) and the topic feeds, and resolves their links. Returns the
    views and the topic feeds' items left out as misfiled (see tag), to archive.
    Raises if the tagger is unavailable.
    """
    catalog = get_catalog()
    if not catalog.topics:
        return {}, []
    results = results_by_source | await poller.fetch_topic_feeds(client, catalog)
    tags_by_title, misfiled = await tag(client, redis_client, catalog, results)
    views = build_topic_views(catalog, results, tags_by_title, time.time(), random.Random())
    left_out = list({h["link"]: h for items in results.values() for h in items if h["title"] in misfiled}.values())
    unique = list({id(h): h for items in views.values() for h in items}.values()) + left_out
    await resolve_links(client, redis_client, unique)
    return views, left_out
