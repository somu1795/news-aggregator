"""
RSS feed fetching, parsing, and weighted headline mixing.

Uses a concurrency semaphore instead of a rate limiter to bound outgoing
requests without artificially throttling throughput.
"""

import asyncio
import calendar
import re
import time
from collections import Counter
from typing import Dict, List, Set
from urllib.parse import urlparse

import feedparser
import httpx
import lxml.etree
import lxml.html
import redis.asyncio as aioredis
import structlog

from config import ALL_VIEW, CATALOG, OTHER_LABEL, Catalog, NewsSource, TopicSource, settings, topic_view
from metrics import FEED_FETCH_FAILURES
from services.link_resolver import resolve_link

logger = structlog.get_logger(__name__)

# Concurrency semaphore — limits parallel outgoing HTTP requests.
# Much more efficient than the previous 20-req/min rate limiter which
# guaranteed 90s+ latency on cache misses with 30+ headlines to resolve.
_OUTGOING_SEMAPHORE = asyncio.Semaphore(15)

# Link resolution talks to Google; a refresh can have ~200 new links (first run),
# so bound it instead of firing them all at once.
_RESOLVE_SEMAPHORE = asyncio.Semaphore(10)

_FEED_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/xml,application/xhtml+xml,text/html;q=0.9, text/plain;q=0.8,*/*;q=0.5",
    "Accept-Language": "en-US,en;q=0.5",
}


def feed_text(entry) -> str:
    """
    The article text the feed itself carries (its full content, or a teaser), as
    plain text. The summarizer falls back on it when the article page can't be read.
    """
    html = max([c.get("value") or "" for c in entry.get("content", [])] + [entry.get("summary") or ""], key=len)
    if not html.strip():
        return ""
    try:
        text = lxml.html.fromstring(html).text_content()
    except (ValueError, lxml.etree.ParserError):
        return ""
    return re.sub(r"\s+", " ", text).strip()[:settings.SUMMARY_MAX_TEXT_CHARS]


def discussion_link(link: str, comments: str | None) -> str | None:
    """The feed's comments URL, unless it's just the article itself (e.g. its "#comments" anchor)."""
    if not comments or urlparse(comments)._replace(fragment="").geturl() == urlparse(link)._replace(fragment="").geturl():
        return None
    return comments


async def fetch_and_parse_feed(
    client: httpx.AsyncClient,
    source_name: str,
    url: str,
) -> List[Dict]:
    """Fetches and parses a single RSS feed with retry logic."""
    for attempt in range(settings.FETCH_RETRIES):
        try:
            async with _OUTGOING_SEMAPHORE:
                response = await client.get(url, headers=_FEED_HEADERS, follow_redirects=True)
            response.raise_for_status()
            feed = feedparser.parse(response.content)

            # Only treat bozo (malformed XML) as fatal if no entries were parsed.
            # Many real-world feeds are technically malformed but usable.
            if getattr(feed, "bozo", False) and not feed.entries:
                raise ValueError(f"Feed parse error for {source_name}: {feed.bozo_exception}")

            headlines = []
            for entry in feed.entries:
                headlines.append({
                    "title": entry.title,
                    "link": entry.link,
                    # Prioritize the source from the article entry, fallback to the feed's name from config
                    "source": getattr(entry, "source", {}).get("title") or source_name,
                    "published": (
                        calendar.timegm(entry.published_parsed)
                        if getattr(entry, "published_parsed", None)
                        else time.time()
                    ),
                    "last_updated": time.time(),
                    "comments": discussion_link(entry.link, entry.get("comments")),
                    "feed_text": feed_text(entry),  # Not published (the API's model has no such field)
                })
            logger.info("Fetched headlines from source", count=len(headlines), source=source_name)
            return headlines
        except Exception as e:
            logger.warning(
                "Feed fetch attempt failed",
                source=source_name, attempt=attempt + 1, error=str(e),
            )
            if attempt < settings.FETCH_RETRIES - 1:
                await asyncio.sleep(settings.BACKOFF_BASE ** attempt)
            else:
                logger.error(
                    "Feed fetch exhausted all retries",
                    source=source_name, retries=settings.FETCH_RETRIES,
                    exc_info=True,
                )
                FEED_FETCH_FAILURES.labels(source=source_name).inc()
                return []
    return []


def _publisher_key(headline: Dict) -> str:
    """
    Identifies the publisher for the per-publisher cap. Google links aren't
    resolved yet at mixing time, so they use the publisher name from the feed;
    direct links (e.g. Hacker News, whose source is always "Hacker News") use
    the link's domain.
    """
    host = (urlparse(headline["link"]).hostname or "").lower()
    if host.endswith("news.google.com"):
        return headline["source"].lower()
    return host.removeprefix("www.")


def _title_key(headline: Dict) -> str:
    """Normalized title for de-duplication; ignores Google's " - Publisher" suffix, case and punctuation."""
    title = headline["title"]
    suffix = f" - {headline['source']}"
    if title.endswith(suffix):
        title = title[: -len(suffix)]
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def mix_headlines(
    sources: List[NewsSource], results_by_source: Dict[str, List[Dict]], now: float, view: str = "",
    cap_publishers: bool = True,
) -> List[Dict]:
    """
    Builds the final list by interleaving sources according to their weights.

    - Each source keeps its own feed order (Google's ranking, HN's front-page rank).
    - Items older than MAX_HEADLINE_AGE_HOURS are dropped.
    - Duplicates (same link or same normalized title) are dropped, first one wins.
    - At most MAX_PER_PUBLISHER items per publisher. A feed whose items all come
      from one publisher (e.g. ABC Australia's own feed) is exempt: its share is
      already bounded by its weight. `cap_publishers=False` skips the cap, for
      combining lists that were already capped when they were built.
    - Sources are merged with smooth weighted round-robin, so every stretch of the
      list has roughly the configured mix (e.g. W S W H W S W W H S ... for 0.5/0.3/0.2)
      and the order is deterministic between refreshes.
    - A source that runs out drops out of the rotation and the others fill its slots.
    """
    min_published = now - settings.MAX_HEADLINE_AGE_HOURS * 3600
    fresh = {
        source.name: [h for h in results_by_source.get(source.name, []) if h["published"] >= min_published]
        for source in sources
    }
    dropped_stale = sum(len(results_by_source.get(name, [])) - len(items) for name, items in fresh.items())
    pending = {name: iter(items) for name, items in fresh.items()}
    single_publisher = {name for name, items in fresh.items() if len({_publisher_key(h) for h in items}) == 1}
    weights = {source.name: source.weight for source in sources}
    credit = {name: 0.0 for name in weights}
    active = [source.name for source in sources]  # Config order breaks ties

    seen_links, seen_titles = set(), set()
    per_publisher = Counter()
    picked_per_source = Counter()
    skipped = Counter()
    mixed: List[Dict] = []

    while active and len(mixed) < settings.MAX_HEADLINES:
        # Smooth weighted round-robin: every active source earns its weight,
        # the one with the most credit is picked and pays back the active total.
        for name in active:
            credit[name] += weights[name]
        name = max(active, key=lambda n: credit[n])
        credit[name] -= sum(weights[n] for n in active)

        for headline in pending[name]:
            title_key = _title_key(headline)
            publisher = _publisher_key(headline)
            if headline["link"] in seen_links or title_key in seen_titles:
                skipped["duplicate"] += 1
            elif cap_publishers and name not in single_publisher and per_publisher[publisher] >= settings.MAX_PER_PUBLISHER:
                skipped["publisher_cap"] += 1
            else:
                seen_links.add(headline["link"])
                seen_titles.add(title_key)
                per_publisher[publisher] += 1
                picked_per_source[name] += 1
                mixed.append(headline)
                break
        else:
            active.remove(name)  # Exhausted; its share goes to the remaining sources

    logger.info(
        "Mixed headlines",
        view=view,
        total=len(mixed),
        per_source=dict(picked_per_source),
        dropped_stale=dropped_stale,
        dropped_duplicate=skipped["duplicate"],
        dropped_publisher_cap=skipped["publisher_cap"],
    )
    return mixed


async def _fetch_all(client: httpx.AsyncClient, sources: List[NewsSource | TopicSource]) -> Dict[str, List[Dict]]:
    """Fetches every feed concurrently; a feed that errors contributes nothing."""
    results = await asyncio.gather(
        *(fetch_and_parse_feed(client, source.name, source.url) for source in sources), return_exceptions=True,
    )
    results_by_source: Dict[str, List[Dict]] = {}
    for source, result in zip(sources, results):
        if isinstance(result, Exception):
            logger.error("Unexpected error fetching source", source=source.name, error=str(result))
            results_by_source[source.name] = []
        else:
            results_by_source[source.name] = result
    return results_by_source


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
        region.id: mix_headlines(region.sources, results_by_source, now, view=f"{region.id}/news")
        for region in catalog.regions
    }
    world = mix_headlines(
        [NewsSource(name=f"region:{region_id}", url="", weight=1 / len(regional)) for region_id in regional],
        {f"region:{region_id}": items for region_id, items in regional.items()},
        now,
        view=f"{ALL_VIEW}/news",
    )
    extras = {source.name: mix_headlines([source], results_by_source, now, view=source.name) for source in catalog.extras}
    news_weight = 1 - sum(source.weight for source in catalog.extras)

    def with_extras(view: str, news: List[Dict]) -> List[Dict]:
        if not news:
            return []
        sources = [NewsSource(name="news", url="", weight=news_weight)] + catalog.extras
        return mix_headlines(sources, {"news": news} | extras, now, view=view, cap_publishers=False)

    views = {region_id: with_extras(region_id, items) for region_id, items in regional.items()}
    views[ALL_VIEW] = with_extras(ALL_VIEW, world)
    return views


async def _resolve_links(client: httpx.AsyncClient, redis_client: aioredis.Redis, headlines: List[Dict]) -> None:
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


async def get_fresh_views(
    client: httpx.AsyncClient,
    redis_client: aioredis.Redis,
) -> tuple[Dict[str, List[Dict]], Dict[str, List[Dict]]]:
    """
    Fetches every feed in the catalog once, builds each region's list and the
    "all" list (see build_views), and resolves links.
    Returns ({view_id: headlines}, {source name: fetched headlines}).
    """
    catalog = CATALOG
    sources = [s for region in catalog.regions for s in region.sources] + catalog.extras
    results_by_source = await _fetch_all(client, sources)

    views = build_views(catalog, results_by_source, time.time())
    if not any(views.values()):
        logger.error("Failed to fetch any headlines from any source")
        return views, results_by_source

    # The same headline object can appear in a region and in "all"; resolve each once
    unique = list({id(h): h for items in views.values() for h in items}.values())
    await _resolve_links(client, redis_client, unique)
    return views, results_by_source


# Last fetch of the topic feeds: {"at": monotonic time, "results": {source name: headlines}}
_topic_feeds: Dict = {"at": float("-inf"), "results": {}}


async def _fetch_topic_feeds(client: httpx.AsyncClient, catalog: Catalog) -> Dict[str, List[Dict]]:
    """Topic feeds change slowly and are many, so they're fetched every TOPIC_FEEDS_INTERVAL_SECONDS."""
    if time.monotonic() - _topic_feeds["at"] >= settings.TOPIC_FEEDS_INTERVAL_SECONDS:
        results = await _fetch_all(client, catalog.topic_feeds)
        if any(results.values()):  # Otherwise keep the previous fetch and retry next cycle
            _topic_feeds.update(at=time.monotonic(), results=results)
    return _topic_feeds["results"]


async def _tag(client: httpx.AsyncClient, catalog: Catalog, results_by_source: Dict[str, List[Dict]]) -> Dict[str, Set[str]]:
    """
    Tags every fetched headline: the tagger's topics, plus the topic its feed is
    about (a Sports section's items are always "sport"). The feed's label is
    also sent to the tagger, which stores it as training data.
    Returns {title: topics}.
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
    logger.info("Tagged headlines", count=len(payload), model=body["model"])
    return {
        item["title"]: set(tags) | ({item["label"]} if item["label"] not in (None, OTHER_LABEL) else set())
        for item, tags in zip(payload, body["tags"])
    }


# Topics whose model tags are too unreliable to show: "uplifting" is a tone, and
# even its most confident tags were about half wrong on live news.
FEED_ONLY_TOPICS = {"uplifting"}


def build_topic_views(
    catalog: Catalog, results_by_source: Dict[str, List[Dict]], tags_by_title: Dict[str, Set[str]], now: float,
) -> Dict[str, List[Dict]]:
    """
    Builds one view per region (and "all") per topic, from the items tagged with
    that topic. The news/extras mix of the main views doesn't apply to a single
    topic (Hacker News has little sport). Instead, items come in this order:

    1. The feeds about the topic (their items are tagged by their feed, so
       they're precise), mixed equally. In a region's view: first the stories
       only that region's own feeds have, then the shared feeds (extras, topic
       extras), which are the same in every region, then global stories: those
       in two or more regions' own feeds (e.g. a TechCrunch story in several
       Google News Technology editions). So region views start with what's
       particular to the region, and repeat each other only further down.
    2. Items the model tagged from other feeds, in the same order, only to
       fill the view, and never for FEED_ONLY_TOPICS.

    Empty views are kept: a region can have no uplifting news right now.
    """
    shared = catalog.extras + catalog.topic_extras
    own = {region.id: region.topic_sources + region.sources for region in catalog.regions}
    everything = [s for sources in own.values() for s in sources] + shared

    def equal(sources) -> List[NewsSource]:
        return [NewsSource(name=s.name, url=s.url, weight=1.0) for s in sources]

    def view_for(groups, topic: str, view: str) -> List[Dict]:
        """groups: (sources, their tagged items), in order of preference."""
        lists = [mix_headlines(equal(s for s in sources if s.label == topic), items, now, view=view) for sources, items in groups]
        if topic not in FEED_ONLY_TOPICS:
            lists += [mix_headlines(equal(s for s in sources if s.label != topic), items, now, view=view) for sources, items in groups]
        seen, combined = set(), []
        for items in lists:
            for h in items:
                keys = {h["link"], _title_key(h)}
                if seen.isdisjoint(keys):
                    seen |= keys
                    combined.append(h)
        return combined[:settings.MAX_HEADLINES]

    views = {}
    for topic in catalog.topics:
        tagged = {
            name: [h for h in items if topic.id in tags_by_title.get(h["title"], ())]
            for name, items in results_by_source.items()
        }
        regions_of = Counter(
            key for sources in own.values()
            for key in {_title_key(h) for s in sources for h in tagged.get(s.name, [])}
        )
        regional = {name: [h for h in items if regions_of[_title_key(h)] < 2] for name, items in tagged.items()}
        global_ = {name: [h for h in items if regions_of[_title_key(h)] >= 2] for name, items in tagged.items()}
        for region in catalog.regions:
            view = topic_view(region.id, topic.id)
            views[view] = view_for([(own[region.id], regional), (shared, tagged), (own[region.id], global_)], topic.id, view)
        view = topic_view(ALL_VIEW, topic.id)
        views[view] = view_for([(everything, tagged)], topic.id, view)
    return views


async def get_topic_views(
    client: httpx.AsyncClient,
    redis_client: aioredis.Redis,
    results_by_source: Dict[str, List[Dict]],
) -> Dict[str, List[Dict]]:
    """
    Builds every topic view from this cycle's news (results_by_source, from
    get_fresh_views) and the topic feeds, and resolves their links.
    Raises if the tagger is unavailable.
    """
    catalog = CATALOG
    if not catalog.topics:
        return {}
    results = results_by_source | await _fetch_topic_feeds(client, catalog)
    tags_by_title = await _tag(client, catalog, results)
    views = build_topic_views(catalog, results, tags_by_title, time.time())
    unique = list({id(h): h for items in views.values() for h in items}.values())
    await _resolve_links(client, redis_client, unique)
    return views
