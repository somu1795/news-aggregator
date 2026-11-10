"""
Mixing several sources' headlines into one view: by weight, de-duplicated,
capped per publisher and per source (see mix_headlines).
"""

import random
import re
from collections import Counter
from typing import Dict, List, Optional
from urllib.parse import urlparse

import structlog

from catalog import NewsSource
from config import settings

logger = structlog.get_logger(__name__)


def publisher_key(headline: Dict) -> str:
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


def title_key(headline: Dict) -> str:
    """Normalized title for de-duplication; ignores Google's " - Publisher" suffix, case and punctuation."""
    title = headline["title"]
    suffix = f" - {headline['source']}"
    if title.endswith(suffix):
        title = title[: -len(suffix)]
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def build_limit() -> int:
    """How long views are built: room for the stories that grouping folds into others."""
    return settings.MAX_HEADLINES + settings.STORY_GROUP_HEADROOM


def mix_headlines(
    sources: List[NewsSource], results_by_source: Dict[str, List[Dict]], now: float, view: str = "",
    cap_publishers: bool = True, max_age_hours: Optional[int] = None, limit: Optional[int] = None,
    rng: Optional[random.Random] = None,
) -> List[Dict]:
    """
    Builds the final list by interleaving sources according to their weights.

    - Each source keeps its own feed order (Google's ranking, HN's front-page rank).
    - Items older than max_age_hours (default MAX_HEADLINE_AGE_HOURS) are dropped.
    - Duplicates (same link or same normalized title) are dropped, first one wins.
    - At most a source's max_items items, when it sets one.
    - At most MAX_PER_PUBLISHER items per publisher. A feed whose items all come
      from one publisher (e.g. ABC Australia's own feed) is exempt: its share is
      already bounded by its weight. `cap_publishers=False` skips the cap, for
      combining lists that were already capped when they were built.
    - Sources are merged with smooth weighted round-robin, so every stretch of the
      list has roughly the configured mix (e.g. W S W H W S W W H S ... for 0.5/0.3/0.2).
      With `rng` (the refresher's, a new one each cycle) the round-robin starts at a random
      point, so any source can lead, the heaviest most often; without it, the first source
      in the config always leads and the order is the same every cycle.
    - A source that runs out drops out of the rotation and the others fill its slots.
    - At most `limit` items (default MAX_HEADLINES). Views are built longer (build_limit())
      and cut to MAX_HEADLINES once each story is shown once (services/story_groups.py).
    """
    limit = limit or settings.MAX_HEADLINES
    min_published = now - (max_age_hours or settings.MAX_HEADLINE_AGE_HOURS) * 3600
    fresh = {
        source.name: [h for h in results_by_source.get(source.name, []) if h["published"] >= min_published]
        for source in sources
    }
    dropped_stale = sum(len(results_by_source.get(name, [])) - len(items) for name, items in fresh.items())
    pending = {name: iter(items) for name, items in fresh.items()}
    single_publisher = {name for name, items in fresh.items() if len({publisher_key(h) for h in items}) == 1}
    weights = {source.name: source.weight for source in sources}
    max_items = {source.name: source.max_items for source in sources if source.max_items is not None}
    total = sum(weights.values())
    credit = {name: rng.random() * total if rng else 0.0 for name in weights}
    active = [source.name for source in sources]  # Config order breaks ties

    seen_links, seen_titles = set(), set()
    per_publisher = Counter()
    picked_per_source = Counter()
    skipped = Counter()
    mixed: List[Dict] = []

    while active and len(mixed) < limit:
        # Smooth weighted round-robin: every active source earns its weight,
        # the one with the most credit is picked and pays back the active total.
        for name in active:
            credit[name] += weights[name]
        name = max(active, key=lambda n: credit[n])
        credit[name] -= sum(weights[n] for n in active)

        if picked_per_source[name] >= max_items.get(name, limit):
            active.remove(name)  # Capped; its share goes to the remaining sources
            continue
        for headline in pending[name]:
            title = title_key(headline)
            publisher = publisher_key(headline)
            if headline["link"] in seen_links or title in seen_titles:
                skipped["duplicate"] += 1
            elif cap_publishers and name not in single_publisher and per_publisher[publisher] >= settings.MAX_PER_PUBLISHER:
                skipped["publisher_cap"] += 1
            else:
                seen_links.add(headline["link"])
                seen_titles.add(title)
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
