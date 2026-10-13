"""
One item per story in each view.

Many outlets cover the same story ("Apple unveils HomePod mini 2", "Apple
launches HomePod mini 2 with Siri AI for $149") under different titles. The
tagger groups headlines by story (POST /group: a multilingual sentence
embedding, see tagger/tagger.py). In each view the first-ranked headline of a
story stays, and its other headlines in that view are folded under it as
"related", so the view's later stories move up. Views are built longer than
MAX_HEADLINES (feed_service.build_limit()) and cut to it here.

Grouping is optional: if the tagger fails, views are published ungrouped.
"""

from typing import Dict, List

import httpx
import structlog

from config import settings

logger = structlog.get_logger(__name__)


def _in_rank_order(views: Dict[str, List[Dict]]) -> List[Dict]:
    """Every distinct headline, top positions of every view first (so a story's group leader is a top item)."""
    seen, ordered = set(), []
    for rank in range(max((len(items) for items in views.values()), default=0)):
        for items in views.values():
            if rank < len(items) and items[rank]["title"] not in seen:
                seen.add(items[rank]["title"])
                ordered.append(items[rank])
    return ordered


def collapse(items: List[Dict], group_of: Dict[str, int]) -> List[Dict]:
    """
    Keeps each story's first headline (a copy, as views share item dicts) with the
    others under "related", each with its discussion page when it has one (Hacker News).
    """
    kept, leaders = [], {}
    for item in items:
        group = group_of.get(item["title"])
        leader = leaders.get(group) if group is not None else None
        if leader is None:
            leader = {**item, "related": []}
            if group is not None:
                leaders[group] = leader
            kept.append(leader)
        elif len(leader["related"]) < settings.MAX_RELATED:
            leader["related"].append({"title": item["title"], "link": item["link"], "source": item["source"],
                                      "comments": item.get("comments")})
    return kept


async def group_stories(client: httpx.AsyncClient, views: Dict[str, List[Dict]]) -> Dict[str, List[Dict]]:
    """Each view with one item per story, cut to MAX_HEADLINES."""
    group_of: Dict[str, int] = {}
    headlines = _in_rank_order(views)
    if headlines:
        try:
            response = await client.post(
                f"{settings.TAGGER_URL}/group",
                json={"items": [{"title": h["title"], "source": h["source"], "link": h["link"]} for h in headlines]},
                timeout=settings.STORY_GROUP_TIMEOUT,
            )
            response.raise_for_status()
            group_of = {h["title"]: g for h, g in zip(headlines, response.json()["groups"])}
        except Exception as e:  # Optional: publish ungrouped rather than not at all
            logger.warning("Stories not grouped", error=str(e) or type(e).__name__)
    grouped = {view: collapse(items, group_of)[:settings.MAX_HEADLINES] for view, items in views.items()}
    if group_of:
        folded = sum(len(item["related"]) for items in grouped.values() for item in items)
        logger.info("Grouped stories", headlines=len(headlines), stories=len(set(group_of.values())), folded=folded)
    return grouped
