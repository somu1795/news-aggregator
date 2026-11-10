"""
Headlines leave a tab's top positions after a while there (turn_over).

The mix follows each feed's own ranking, and a feed's top story can stay its
top story all day (on Nov 8-10, 2026, the main tab's first headline stayed
first for up to 13 hours, and only 2 of its top 10 changed in 6 hours). So
each tab remembers when each of its headlines first reached its first
TOP_POSITIONS (a story by its id, so another of its headlines leading it later
doesn't start its time again). After TOP_DWELL_SECONDS there, a headline can't
be in them again while it stays in the tab: the next ones move up, and it keeps
its place among the rest.
"""

from typing import Dict, List, Tuple

import redis.asyncio as aioredis

from config import settings
from keys import top_since_key


def _key(item: Dict) -> str:
    return f"story:{item['story']}" if item.get("story") is not None else item["link"]


def wear_off(items: List[Dict], since: Dict[str, float], now: float) -> Tuple[List[Dict], Dict[str, float]]:
    """
    The items with those that have had their time on top out of the first TOP_POSITIONS, and
    when each item now in the tab first reached them (those never on top aren't in it).
    """
    done = {i for i, item in enumerate(items) if now - since.get(_key(item), now) >= settings.TOP_DWELL_SECONDS}
    top = [i for i in range(len(items)) if i not in done][:settings.TOP_POSITIONS]
    rest = [i for i in range(len(items)) if i not in set(top)]
    ordered = [items[i] for i in top + rest]
    present = {_key(item) for item in items}
    after = {key: t for key, t in since.items() if key in present}
    for item in ordered[:settings.TOP_POSITIONS]:
        after.setdefault(_key(item), now)
    return ordered, after


async def turn_over(redis_client: aioredis.Redis, views: Dict[str, List[Dict]], now: float) -> Dict[str, List[Dict]]:
    """Each view after wear_off, with when its headlines reached its top kept in Redis (a day after its last refresh)."""
    names = list(views)
    async with redis_client.pipeline(transaction=False) as pipe:
        for view in names:
            pipe.hgetall(top_since_key(view))
        stored = await pipe.execute()
    result = {}
    async with redis_client.pipeline(transaction=False) as pipe:
        for view, since in zip(names, stored):
            result[view], after = wear_off(views[view], {k: float(v) for k, v in since.items()}, now)
            pipe.delete(top_since_key(view))
            if after:
                pipe.hset(top_since_key(view), mapping=after)
                pipe.expire(top_since_key(view), 86400)
        await pipe.execute()
    return result
