"""Tests for polite feed polling: conditional requests, per-feed intervals, host backoff."""

import httpx
import pytest
import respx

from config import settings
from services.feeds import poller as poller_module

pytestmark = pytest.mark.asyncio

FEED = "https://outlet.example/rss"


def rss(*titles):
    items = "".join(f"<item><title>{t}</title><link>https://outlet.example/{t}</link>"
                    f"<pubDate>Tue, 13 Oct 2026 10:00:00 GMT</pubDate></item>" for t in titles)
    return f"<rss><channel><title>Outlet</title>{items}</channel></rss>".encode()


def advance(monkeypatch, seconds):
    """Moves the refresher's clock forward."""
    now = poller_module.time.monotonic() + seconds
    monkeypatch.setattr(poller_module.time, "monotonic", lambda: now)


@respx.mock
async def test_unchanged_feeds_answer_304_and_their_headlines_are_reused(poller):
    route = respx.get(FEED).mock(side_effect=[
        httpx.Response(200, content=rss("a", "b"), headers={"ETag": '"v1"', "Last-Modified": "Tue, 13 Oct 2026 10:00:00 GMT"}),
        httpx.Response(304),
    ])
    async with httpx.AsyncClient() as client:
        first = await poller.fetch(client, "Outlet", FEED)
        poller.feeds[FEED].next_fetch = 0  # Due again now
        second = await poller.fetch(client, "Outlet", FEED)
    assert [h["title"] for h in second] == [h["title"] for h in first] == ["a", "b"]
    sent = route.calls[1].request.headers
    assert sent["If-None-Match"] == '"v1"' and sent["If-Modified-Since"] == "Tue, 13 Oct 2026 10:00:00 GMT"


@respx.mock
async def test_a_quiet_feed_is_fetched_less_often_and_a_busy_one_every_cycle(poller, monkeypatch):
    route = respx.get(FEED).mock(side_effect=[httpx.Response(200, content=rss("a")),
                                              httpx.Response(200, content=rss("a")),  # Nothing new
                                              httpx.Response(200, content=rss("a", "b"))])  # New story
    cycle = settings.REFRESH_INTERVAL_SECONDS
    async with httpx.AsyncClient() as client:
        await poller.fetch(client, "Outlet", FEED)  # First fetch: new, so every cycle
        advance(monkeypatch, cycle)
        await poller.fetch(client, "Outlet", FEED)  # Nothing new: wait one more cycle
        advance(monkeypatch, cycle)
        assert [h["title"] for h in await poller.fetch(client, "Outlet", FEED)] == ["a"]  # Not due: reused
        assert route.call_count == 2
        advance(monkeypatch, cycle)
        await poller.fetch(client, "Outlet", FEED)  # Due; brings "b"
        assert route.call_count == 3 and poller.feeds[FEED].interval == 0


@respx.mock
async def test_a_429_pauses_every_feed_on_that_host_without_retrying(poller, monkeypatch):
    other = "https://outlet.example/sport/rss"
    first = respx.get(FEED).mock(side_effect=[httpx.Response(200, content=rss("a")),
                                              httpx.Response(429, headers={"Retry-After": "120"}),
                                              httpx.Response(200, content=rss("a", "b"))])
    second = respx.get(other).mock(return_value=httpx.Response(200, content=rss("s")))
    async with httpx.AsyncClient() as client:
        await poller.fetch(client, "Outlet", FEED)
        poller.feeds[FEED].next_fetch = 0
        assert [h["title"] for h in await poller.fetch(client, "Outlet", FEED)] == ["a"]  # Last headlines
        assert first.call_count == 2  # No retries on 429
        assert await poller.fetch(client, "Outlet sport", other) == [] and second.call_count == 0  # Same host: paused
        advance(monkeypatch, 121)
        poller.feeds[FEED].next_fetch = 0
        assert [h["title"] for h in await poller.fetch(client, "Outlet", FEED)] == ["a", "b"]


@respx.mock
async def test_without_retry_after_the_pause_doubles(poller, monkeypatch):
    respx.get(FEED).mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as client:
        for _ in range(3):
            await poller.fetch(client, "Outlet", FEED)
            advance(monkeypatch, settings.FEED_BACKOFF_MAX_SECONDS + 1)
    assert poller.backoff["outlet.example"].strikes == 3


@respx.mock
async def test_a_failing_feed_keeps_its_last_headlines(poller, monkeypatch):
    monkeypatch.setattr(settings, "FETCH_RETRIES", 1)
    respx.get(FEED).mock(side_effect=[httpx.Response(200, content=rss("a")), httpx.ConnectError("down")])
    async with httpx.AsyncClient() as client:
        await poller.fetch(client, "Outlet", FEED)
        poller.feeds[FEED].next_fetch = 0
        assert [h["title"] for h in await poller.fetch(client, "Outlet", FEED)] == ["a"]


def test_a_feed_item_without_a_title_or_link_is_skipped():
    from services.feeds.parse import parse
    feed = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Feed</title>
        <item><title>Bridge approved</title><link>https://pub.example/1</link></item>
        <item><link>https://pub.example/2</link><description>No title</description></item>
        <item><title>No link</title></item>
        </channel></rss>"""
    assert [h["title"] for h in parse(feed, "Feed")] == ["Bridge approved"]


def test_a_photo_credit_in_an_items_source_is_not_taken_for_its_publisher():
    # France 24 puts each photo's credit in <source>, pointing at the photo; Google News puts the publisher there
    from services.feeds.parse import parse
    feed = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Feed</title>
        <item><title>Credit</title><link>https://pub.example/1</link>
            <source url="https://img.example/display/1.jpg">\xc2\xa9 Fred Tanneau, AFP</source></item>
        <item><title>Credit without the sign</title><link>https://pub.example/2</link>
            <source url="https://img.example/w:1024/2.JPEG">Stringer, Reuters</source></item>
        <item><title>Publisher</title><link>https://pub.example/3</link>
            <source url="https://www.bbc.co.uk">BBC</source></item>
        </channel></rss>"""
    assert [h["source"] for h in parse(feed, "France 24 Europe")] == ["France 24 Europe", "France 24 Europe", "BBC"]
