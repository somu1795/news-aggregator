"""Tests for polite feed polling: conditional requests, per-feed intervals, host backoff."""

import httpx
import pytest
import respx

from config import settings
from services import feed_service
from services.feed_service import fetch_and_parse_feed

pytestmark = pytest.mark.asyncio

FEED = "https://outlet.example/rss"


def rss(*titles):
    items = "".join(f"<item><title>{t}</title><link>https://outlet.example/{t}</link>"
                    f"<pubDate>Tue, 13 Oct 2026 10:00:00 GMT</pubDate></item>" for t in titles)
    return f"<rss><channel><title>Outlet</title>{items}</channel></rss>".encode()


def advance(monkeypatch, seconds):
    """Moves the refresher's clock forward."""
    now = feed_service.time.monotonic() + seconds
    monkeypatch.setattr(feed_service.time, "monotonic", lambda: now)


@respx.mock
async def test_unchanged_feeds_answer_304_and_their_headlines_are_reused():
    route = respx.get(FEED).mock(side_effect=[
        httpx.Response(200, content=rss("a", "b"), headers={"ETag": '"v1"', "Last-Modified": "Tue, 13 Oct 2026 10:00:00 GMT"}),
        httpx.Response(304),
    ])
    async with httpx.AsyncClient() as client:
        first = await fetch_and_parse_feed(client, "Outlet", FEED)
        feed_service._feeds[FEED].next_fetch = 0  # Due again now
        second = await fetch_and_parse_feed(client, "Outlet", FEED)
    assert [h["title"] for h in second] == [h["title"] for h in first] == ["a", "b"]
    sent = route.calls[1].request.headers
    assert sent["If-None-Match"] == '"v1"' and sent["If-Modified-Since"] == "Tue, 13 Oct 2026 10:00:00 GMT"


@respx.mock
async def test_a_quiet_feed_is_fetched_less_often_and_a_busy_one_every_cycle(monkeypatch):
    route = respx.get(FEED).mock(side_effect=[httpx.Response(200, content=rss("a")),
                                              httpx.Response(200, content=rss("a")),  # Nothing new
                                              httpx.Response(200, content=rss("a", "b"))])  # New story
    cycle = settings.REFRESH_INTERVAL_SECONDS
    async with httpx.AsyncClient() as client:
        await fetch_and_parse_feed(client, "Outlet", FEED)  # First fetch: new, so every cycle
        advance(monkeypatch, cycle)
        await fetch_and_parse_feed(client, "Outlet", FEED)  # Nothing new: wait one more cycle
        advance(monkeypatch, cycle)
        assert [h["title"] for h in await fetch_and_parse_feed(client, "Outlet", FEED)] == ["a"]  # Not due: reused
        assert route.call_count == 2
        advance(monkeypatch, cycle)
        await fetch_and_parse_feed(client, "Outlet", FEED)  # Due; brings "b"
        assert route.call_count == 3 and feed_service._feeds[FEED].interval == 0


@respx.mock
async def test_a_429_pauses_every_feed_on_that_host_without_retrying(monkeypatch):
    other = "https://outlet.example/sport/rss"
    first = respx.get(FEED).mock(side_effect=[httpx.Response(200, content=rss("a")),
                                              httpx.Response(429, headers={"Retry-After": "120"}),
                                              httpx.Response(200, content=rss("a", "b"))])
    second = respx.get(other).mock(return_value=httpx.Response(200, content=rss("s")))
    async with httpx.AsyncClient() as client:
        await fetch_and_parse_feed(client, "Outlet", FEED)
        feed_service._feeds[FEED].next_fetch = 0
        assert [h["title"] for h in await fetch_and_parse_feed(client, "Outlet", FEED)] == ["a"]  # Last headlines
        assert first.call_count == 2  # No retries on 429
        assert await fetch_and_parse_feed(client, "Outlet sport", other) == [] and second.call_count == 0  # Same host: paused
        advance(monkeypatch, 121)
        feed_service._feeds[FEED].next_fetch = 0
        assert [h["title"] for h in await fetch_and_parse_feed(client, "Outlet", FEED)] == ["a", "b"]


@respx.mock
async def test_without_retry_after_the_pause_doubles(monkeypatch):
    respx.get(FEED).mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as client:
        for _ in range(3):
            await fetch_and_parse_feed(client, "Outlet", FEED)
            advance(monkeypatch, settings.FEED_BACKOFF_MAX_SECONDS + 1)
    assert feed_service._backoff["outlet.example"].strikes == 3


@respx.mock
async def test_a_failing_feed_keeps_its_last_headlines(monkeypatch):
    monkeypatch.setattr(settings, "FETCH_RETRIES", 1)
    respx.get(FEED).mock(side_effect=[httpx.Response(200, content=rss("a")), httpx.ConnectError("down")])
    async with httpx.AsyncClient() as client:
        await fetch_and_parse_feed(client, "Outlet", FEED)
        feed_service._feeds[FEED].next_fetch = 0
        assert [h["title"] for h in await fetch_and_parse_feed(client, "Outlet", FEED)] == ["a"]
