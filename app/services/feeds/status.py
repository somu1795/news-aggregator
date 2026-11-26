"""
The monthly review's view of the feeds (MAINTENANCE.md): those not working now, from the state the
refresher keeps for each in Redis (FeedPoller.check_health). Read the state, not the logs: a deploy
recreates the container and its logs with it.

    docker compose exec refresher python -m services.feeds.status
"""

import asyncio
import json
import time

import redis.asyncio as aioredis

from catalog import Catalog, get_catalog
from keys import feed_state_key


async def report(redis_client: aioredis.Redis, catalog: Catalog) -> str:
    """Each feed of the catalog not working now: for how long, its last error, its address."""
    now, lines = time.time(), []
    for source in catalog.feeds:
        raw = await redis_client.get(feed_state_key(source.url))
        saved = json.loads(raw) if raw else {}
        if not saved.get("reported"):
            continue
        days = int((now - (saved.get("answered_at") or saved.get("watched_since") or now)) // 86400)
        lines.append(f"- {source.name}: no usable answer for {days} day{'' if days == 1 else 's'}, "
                     f"last error: {saved.get('error')}\n  {source.url}")
    if not lines:
        return "Every feed is working.\n"
    return "Not working:\n" + "\n".join(lines) + "\n"


async def main() -> None:
    from services.redis_client import create_redis_client
    redis_client = create_redis_client(socket_timeout=5, retries=1)
    print(await report(redis_client, get_catalog()), end="")
    await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
