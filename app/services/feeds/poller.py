"""
Fetching feeds politely. The refresher keeps one FeedPoller for its whole
life (a single long-lived process): what it remembers about each feed and
host between cycles is what makes polling polite (see FeedPoller.fetch).

A concurrency semaphore, not a rate limiter, bounds outgoing requests without
throttling throughput.
"""

import asyncio
import email.utils
import random
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from urllib.parse import urlparse

import httpx
import structlog

from catalog import Catalog, NewsSource, TopicSource
from config import settings
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
    """

    def __init__(self):
        self.backoff: Dict[str, _HostBackoff] = {}  # By host
        self.next_turn: Dict[str, float] = {}  # By host: when its next request may start (time.monotonic())

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


class FeedPoller(HostLimits):
    """Every feed's last headlines and validators, and every host's pause, kept between cycles."""

    def __init__(self):
        super().__init__()
        self.feeds: Dict[str, _FeedState] = {}  # By feed URL
        # Last fetch of the topic feeds: {"at": monotonic time, "results": {source name: headlines}}
        self.topic_feeds: Dict = {"at": float("-inf"), "results": {}}

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
        - a feed that can't be fetched is asked less often, like a quiet one.
        When a feed can't be fetched, its last headlines are reused (they still age
        out after MAX_HEADLINE_AGE_HOURS).
        """
        state = self.feeds.setdefault(url, _FeedState())
        host = urlparse(url).hostname or ""
        if self.paused(host) or time.monotonic() < state.next_fetch:
            return state.items
        await asyncio.sleep(random.uniform(0, settings.FEED_JITTER_SECONDS))
        headers = dict(_FEED_HEADERS)
        if state.etag:
            headers["If-None-Match"] = state.etag
        if state.last_modified:
            headers["If-Modified-Since"] = state.last_modified
        for attempt in range(settings.FETCH_RETRIES):
            await self.take_turn(host, settings.FEED_HOST_GAP_SECONDS)
            if self.paused(host):  # Told to slow down while waiting
                return state.items
            try:
                async with _OUTGOING_SEMAPHORE:
                    response = await client.get(url, headers=headers, follow_redirects=True)
                if response.status_code in (429, 503):
                    seconds = self.back_off(host, response)
                    logger.warning("Feed host asked us to slow down, pausing it", source=source_name, host=host,
                                   status=response.status_code, seconds=round(seconds))
                    return state.items
                self.answered(host)
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
                # Other 4xx, or a page that isn't a feed (a bot check's): asking again at once won't help
                if isinstance(e, ValueError) or isinstance(e, httpx.HTTPStatusError) and e.response.status_code < 500:
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
        return results_by_source

    async def fetch_topic_feeds(self, client: httpx.AsyncClient, catalog: Catalog) -> Dict[str, List[Dict]]:
        """Topic feeds change slowly and are many, so they're fetched every TOPIC_FEEDS_INTERVAL_SECONDS."""
        if time.monotonic() - self.topic_feeds["at"] >= settings.TOPIC_FEEDS_INTERVAL_SECONDS:
            results = await self.fetch_all(client, catalog.topic_feeds)
            if any(results.values()):  # Otherwise keep the previous fetch and retry next cycle
                self.topic_feeds.update(at=time.monotonic(), results=results)
        return self.topic_feeds["results"]
