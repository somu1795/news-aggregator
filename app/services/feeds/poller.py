"""
Fetching feeds politely. The refresher keeps one FeedPoller for its whole
life (a single long-lived process): what it remembers about each feed and
host between cycles is what makes polling polite (see FeedPoller.fetch).

A concurrency semaphore, not a rate limiter, bounds outgoing requests without
throttling throughput.
"""

import asyncio
import email.utils
import json
import random
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from urllib.parse import urlparse

import httpx
import redis.asyncio as aioredis
import structlog

from catalog import Catalog, NewsSource, TopicSource
from config import settings
from keys import TOPIC_BATCH_KEY, feed_state_key, host_pause_key
from services.feeds.parse import parse

logger = structlog.get_logger(__name__)

# Concurrency semaphore — limits parallel outgoing HTTP requests.
# Much more efficient than the previous 20-req/min rate limiter which
# guaranteed 90s+ latency on cache misses with 30+ headlines to resolve.
_OUTGOING_SEMAPHORE = asyncio.Semaphore(15)


_FEED_HEADERS = {
    "User-Agent": settings.BROWSER_USER_AGENT,
    "Accept": "application/xml,application/xhtml+xml,text/html;q=0.9, text/plain;q=0.8,*/*;q=0.5",
    "Accept-Language": "en-US,en;q=0.5",
}




@dataclass
class _FeedState:
    """What the refresher remembers about a feed between cycles (it's a single long-lived process)."""
    items: List[Dict] = field(default_factory=list)  # Its last parsed headlines
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    interval: float = 0.0  # Seconds until it's fetched again: 0 is every cycle
    next_fetch: float = 0.0  # time.monotonic()
    plain: bool = False  # Asked as a plain HTTP client, not as a browser (_ask)




@dataclass
class _HostBackoff:
    until: float = 0.0  # time.monotonic()
    strikes: int = 0  # Consecutive 429/503s, for the backoff when there's no Retry-After



def _retry_after(response: httpx.Response) -> Optional[float]:
    """Retry-After in seconds (it's either seconds or an HTTP date), or None."""
    value = response.headers.get("retry-after", "").strip()
    if value.isdigit():
        return float(value)
    try:
        return max(0.0, email.utils.parsedate_to_datetime(value).timestamp() - time.time())
    except (TypeError, ValueError, IndexError):
        return None


def _refused(status: int) -> bool:
    """An answer that may depend on who asks: a 4xx other than gone (404, 410) or slow down (429)."""
    return 400 <= status < 500 and status not in (404, 410, 429)


def _schedule(state: _FeedState, changed: bool) -> None:
    """New headlines: fetch every cycle; none: skip a cycle, then twice as long each time (10, 20, 30 min)."""
    if changed:
        state.interval = 0.0
    else:
        state.interval = min(settings.FEED_MAX_INTERVAL_SECONDS,
                             max(2 * settings.REFRESH_INTERVAL_SECONDS, state.interval * 2))
    # A little early, so a feed due "in 10 minutes" is fetched by the cycle that starts then
    state.next_fetch = time.monotonic() + state.interval - settings.REFRESH_INTERVAL_SECONDS / 4


class HostLimits:
    """
    Being polite to hosts: requests to one start at least a gap apart (take_turn), and one that
    answers 429/503 is paused (back_off: its Retry-After, or 10 min doubling up to 2 h), its
    strikes forgotten at its first answer after the pause (answered).

    Pauses and strikes are also kept in Redis (remember, restore), under a scope of their own
    (the feeds', link resolution's), so a restarted refresher doesn't ask a host that is still
    paused: forgetting it asked Google again 13 s before its pause ended, and drew another 429.
    Memory stays the authority; Redis is written only when a host's pause changes, and if it's
    unavailable a restart just starts without the pauses, as before.
    """

    def __init__(self, scope: str = "feeds"):
        self.scope = scope
        self.backoff: Dict[str, _HostBackoff] = {}  # By host
        self.next_turn: Dict[str, float] = {}  # By host: when its next request may start (time.monotonic())
        self.saved: Dict[str, Optional[tuple]] = {}  # By host: its pause as last written to Redis

    def paused(self, host: str) -> bool:
        return time.monotonic() < self.backoff.get(host, _HostBackoff()).until

    async def take_turn(self, host: str, gap: float) -> None:
        """Waits until this host's next request may start, and books the one after."""
        now = time.monotonic()
        start = max(now, self.next_turn.get(host, now))
        self.next_turn[host] = start + gap
        if start > now:
            await asyncio.sleep(start - now)

    def back_off(self, host: str, response: httpx.Response) -> float:
        """
        Pauses a host that answered 429/503 and returns for how many seconds. Answers to
        requests sent before the pause began don't double it again.
        """
        state = self.backoff.setdefault(host, _HostBackoff())
        if self.paused(host):
            return state.until - time.monotonic()
        state.strikes += 1
        seconds = _retry_after(response)
        if seconds is None:
            seconds = min(settings.FEED_BACKOFF_MAX_SECONDS, settings.FEED_BACKOFF_SECONDS * 2 ** (state.strikes - 1))
        seconds = min(seconds, settings.FEED_BACKOFF_MAX_SECONDS)
        state.until = time.monotonic() + seconds
        return seconds

    def answered(self, host: str) -> None:
        """A host answered other than 429/503: its strikes are forgotten, unless the answer was on its way before a pause."""
        if not self.paused(host):
            self.backoff.pop(host, None)

    async def remember(self, redis_client: aioredis.Redis, host: str) -> None:
        """Writes a host's pause and strikes to Redis if they changed (or deletes them once forgotten)."""
        state = self.backoff.get(host)
        current = (state.until, state.strikes) if state else None
        if current == self.saved.get(host, None):
            return
        try:
            if state:
                left = state.until - time.monotonic()
                # Kept after the pause ends, so the strikes still double the next one if the host refuses again
                await redis_client.set(host_pause_key(self.scope, host),
                                       json.dumps({"until": time.time() + left, "strikes": state.strikes}),
                                       ex=max(1, int(left + settings.FEED_BACKOFF_MAX_SECONDS)))
            else:
                await redis_client.delete(host_pause_key(self.scope, host))
            self.saved[host] = current
        except aioredis.RedisError as e:
            logger.warning("Host pause not saved", host=host, error=str(e))

    async def restore(self, redis_client: aioredis.Redis) -> None:
        """Takes up the pauses and strikes a previous run left in Redis (at a restart)."""
        prefix = host_pause_key(self.scope, "")
        try:
            async for key in redis_client.scan_iter(match=prefix + "*"):
                key = key.decode() if isinstance(key, bytes) else key
                value = await redis_client.get(key)
                if not value:
                    continue
                saved = json.loads(value)
                host = key[len(prefix):]
                self.backoff[host] = _HostBackoff(until=time.monotonic() + saved["until"] - time.time(),
                                                  strikes=int(saved["strikes"]))
                self.saved[host] = (self.backoff[host].until, self.backoff[host].strikes)
                if self.paused(host):
                    logger.info("Host still paused from before the restart", scope=self.scope, host=host,
                                seconds=round(self.backoff[host].until - time.monotonic()))
        except (aioredis.RedisError, ValueError, KeyError, TypeError) as e:
            logger.warning("Host pauses not restored", scope=self.scope, error=str(e))


class FeedPoller(HostLimits):
    """
    Every feed's last headlines, validators and schedule, and every host's pause, kept between cycles; and in
    Redis, so a restarted refresher takes them up (restore). Without them every feed was due at once after a
    restart: ~100 Google feeds asked within minutes drew a 429 and a 10-minute pause on every restart. A feed's
    state is saved after each round of fetches that asked it (save_feeds), without the articles' texts (the
    summarizer's copy is kept apart); if Redis is unavailable, a restart starts afresh, as before.
    """

    def __init__(self, redis_client: Optional[aioredis.Redis] = None):
        super().__init__("feeds")
        self.redis = redis_client
        self.feeds: Dict[str, _FeedState] = {}  # By feed URL
        self.dirty: set = set()  # Feeds asked since their state was last saved
        # Last fetch of the topic feeds: {"at": monotonic time, "results": {source name: headlines},
        # "missed": names of those it left out for a paused host}
        self.topic_feeds: Dict = {"at": float("-inf"), "results": {}, "missed": set()}

    async def fetch(self, client: httpx.AsyncClient, source_name: str, url: str) -> List[Dict]:
        """
        A feed's headlines, fetched politely:
        - each feed has its own interval: a fetch that brings no new headlines doubles
          it (up to FEED_MAX_INTERVAL_SECONDS), new headlines reset it to every cycle;
          in between, its last headlines are reused without a request;
        - conditional requests (ETag / Last-Modified): an unchanged feed answers 304;
        - requests start after a small random delay, and FEED_HOST_GAP_SECONDS apart on
          one host, so they don't all go out at once;
        - a 429 or 503 pauses every feed on that host (Retry-After, or a doubling
          backoff), with no retries, and stops those still waiting their turn; network
          errors and other 5xx are retried with backoff (each retry waits its turn too),
          other 4xx (403, 404: asking again won't help) and pages that aren't feeds aren't;
        - a feed that refuses the browser identity is asked as a plain client, or the other way round (_ask);
        - a feed that can't be fetched is asked less often, like a quiet one.
        When a feed can't be fetched, its last headlines are reused (they still age
        out after MAX_HEADLINE_AGE_HOURS).
        """
        state = self.feeds.setdefault(url, _FeedState())
        host = urlparse(url).hostname or ""
        if self.paused(host) or time.monotonic() < state.next_fetch:
            return state.items
        self.dirty.add(url)
        await asyncio.sleep(random.uniform(0, settings.FEED_JITTER_SECONDS))
        for attempt in range(settings.FETCH_RETRIES):
            await self.take_turn(host, settings.FEED_HOST_GAP_SECONDS)
            if self.paused(host):  # Told to slow down while waiting
                return state.items
            try:
                response = await self._ask(client, source_name, url, state, host)
                if response is None:  # Told to slow down while waiting to ask again
                    return state.items
                if response.status_code in (429, 503):
                    seconds = self.back_off(host, response)
                    logger.warning("Feed host asked us to slow down, pausing it", source=source_name, host=host,
                                   status=response.status_code, seconds=round(seconds))
                    if self.redis:
                        await self.remember(self.redis, host)
                    return state.items
                self.answered(host)
                if self.redis:
                    await self.remember(self.redis, host)  # Only writes if that forgot a pause
                if response.status_code == 304:
                    _schedule(state, changed=False)
                    return state.items
                response.raise_for_status()
                headlines = parse(response.content, source_name)
                break
            except Exception as e:
                logger.warning(
                    "Feed fetch attempt failed",
                    source=source_name, attempt=attempt + 1, error=str(e),
                )
                # Other 4xx, a refusal over HTTP/2 (_ask) or a page that isn't a feed (a bot check's): asking again at once won't help
                if (isinstance(e, (ValueError, httpx.ProtocolError))
                        or isinstance(e, httpx.HTTPStatusError) and e.response.status_code < 500):
                    _schedule(state, changed=False)
                    return state.items
                if attempt < settings.FETCH_RETRIES - 1:
                    await asyncio.sleep(settings.BACKOFF_BASE ** attempt)
                else:
                    _schedule(state, changed=False)
                    logger.error(
                        "Feed fetch exhausted all retries",
                        source=source_name, retries=settings.FETCH_RETRIES,
                        exc_info=True,
                    )
                    return state.items
        known = {h["title"] for h in state.items}  # By title: links are rewritten once resolved (services.links.resolve_links)
        changed = not state.items or any(h["title"] not in known for h in headlines)
        state.items = headlines
        state.etag = response.headers.get("etag")
        state.last_modified = response.headers.get("last-modified")
        _schedule(state, changed)
        logger.info("Fetched headlines from source", count=len(headlines), source=source_name)
        return headlines

    async def _ask(self, client: httpx.AsyncClient, source_name: str, url: str, state: _FeedState,
                   host: str) -> Optional[httpx.Response]:
        """
        Asks for a feed as the client it last answered: a browser (BROWSER_USER_AGENT), or a plain HTTP
        client (httpx's own User-Agent). Refused (_refused), it asks once more at once as the other, and
        keeps to that one. Sites' bot rules differ and change: since Nov 23, 2026 the Guardian refuses a
        client calling itself Chrome without being one (403; over HTTP/2 an error httpx reports as a
        protocol error) and Hacker News answers it 419, while Balkan Insight and Colombia One refuse a
        plain client. None if the host is paused while this waits its turn to ask again.
        """
        try:
            response = await self._get(client, url, state)
            if not _refused(response.status_code):
                return response
        except httpx.ProtocolError:
            pass
        state.plain = not state.plain
        logger.info("Feed asked as another client", source=source_name, plain=state.plain)
        await self.take_turn(host, settings.FEED_HOST_GAP_SECONDS)
        if self.paused(host):
            return None
        return await self._get(client, url, state)

    @staticmethod
    async def _get(client: httpx.AsyncClient, url: str, state: _FeedState) -> httpx.Response:
        headers = {k: v for k, v in _FEED_HEADERS.items() if not (state.plain and k == "User-Agent")}
        if state.etag:
            headers["If-None-Match"] = state.etag
        if state.last_modified:
            headers["If-Modified-Since"] = state.last_modified
        async with _OUTGOING_SEMAPHORE:
            return await client.get(url, headers=headers, follow_redirects=True)

    async def fetch_all(self, client: httpx.AsyncClient, sources: List[NewsSource | TopicSource]) -> Dict[str, List[Dict]]:
        """Fetches every feed concurrently; a feed that errors contributes nothing."""
        results = await asyncio.gather(
            *(self.fetch(client, source.name, source.url) for source in sources), return_exceptions=True,
        )
        results_by_source: Dict[str, List[Dict]] = {}
        for source, result in zip(sources, results):
            if isinstance(result, Exception):
                logger.error("Unexpected error fetching source", source=source.name, error=str(result))
                results_by_source[source.name] = []
            else:
                results_by_source[source.name] = result
        await self.save_feeds()
        return results_by_source

    async def save_feeds(self) -> None:
        """Writes the state of the feeds asked since the last save to Redis (see the class)."""
        if not self.redis or not self.dirty:
            return
        now, mono = time.time(), time.monotonic()
        try:
            async with self.redis.pipeline(transaction=False) as pipe:
                for url in self.dirty:
                    state = self.feeds[url]
                    pipe.set(feed_state_key(url), json.dumps({
                        "url": url, "etag": state.etag, "last_modified": state.last_modified, "interval": state.interval,
                        "plain": state.plain, "next_fetch": now + state.next_fetch - mono,
                        "items": [{k: v for k, v in h.items() if k != "feed_text"} for h in state.items],
                    }), ex=settings.FEED_STATE_TTL_SECONDS)
                await pipe.execute()
            self.dirty.clear()
        except aioredis.RedisError as e:
            logger.warning("Feed states not saved", error=str(e))

    async def restore(self, redis_client: aioredis.Redis) -> None:
        """Takes up the host pauses, feed states and topic batch time a previous run left in Redis (at a restart)."""
        await super().restore(redis_client)
        now, mono = time.time(), time.monotonic()
        try:
            async for key in redis_client.scan_iter(match="feed-state:*"):
                value = await redis_client.get(key)
                if not value:
                    continue
                saved = json.loads(value)
                self.feeds[saved["url"]] = _FeedState(items=saved["items"], etag=saved["etag"], last_modified=saved["last_modified"],
                                                      interval=saved["interval"], next_fetch=mono + saved["next_fetch"] - now,
                                                      plain=saved.get("plain", False))
            batch = await redis_client.get(TOPIC_BATCH_KEY)
            if batch:
                self.topic_feeds["at"] = mono + float(batch) - now
            if self.feeds:
                logger.info("Feed states restored", feeds=len(self.feeds))
        except (aioredis.RedisError, ValueError, KeyError, TypeError) as e:
            logger.warning("Feed states not restored", error=str(e))

    async def fetch_topic_feeds(self, client: httpx.AsyncClient, catalog: Catalog) -> Dict[str, List[Dict]]:
        """
        Topic feeds change slowly and are many, so they're fetched every TOPIC_FEEDS_INTERVAL_SECONDS.
        One a batch left out because its host was paused, with no headlines to reuse (after a
        restart), is asked again every cycle until its host answers, not only at the next batch:
        otherwise a batch that falls in a Google pause leaves the topic tabs without Google's.
        """
        if time.monotonic() - self.topic_feeds["at"] >= settings.TOPIC_FEEDS_INTERVAL_SECONDS:
            results = await self.fetch_all(client, catalog.topic_feeds)
            if any(results.values()):  # Otherwise keep the previous fetch and retry next cycle
                self.topic_feeds.update(at=time.monotonic(), results=results,
                                        missed=self._missed(catalog.topic_feeds, results))
                if self.redis:
                    try:
                        await self.redis.set(TOPIC_BATCH_KEY, str(time.time()), ex=settings.FEED_STATE_TTL_SECONDS)
                    except aioredis.RedisError as e:
                        logger.warning("Topic batch time not saved", error=str(e))
        elif not self.topic_feeds["results"]:  # Restarted between batches: the feeds' restored headlines
            self.topic_feeds["results"] = {s.name: self.feeds[s.url].items if s.url in self.feeds else [] for s in catalog.topic_feeds}
        elif self.topic_feeds["missed"]:
            sources = [s for s in catalog.topic_feeds if s.name in self.topic_feeds["missed"]]
            results = await self.fetch_all(client, sources)
            self.topic_feeds.update(results=self.topic_feeds["results"] | results, missed=self._missed(sources, results))
        return self.topic_feeds["results"]

    def _missed(self, sources: List[NewsSource | TopicSource], results: Dict[str, List[Dict]]) -> set:
        """The feeds that came back empty because their host is paused."""
        return {s.name for s in sources if not results[s.name] and self.paused(urlparse(s.url).hostname or "")}
