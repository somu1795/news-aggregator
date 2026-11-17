"""Tests for polite feed polling: conditional requests, per-feed intervals, host backoff."""

import time
from types import SimpleNamespace

import httpx
import pytest
import respx

from config import settings
from services.feeds import poller as poller_module
from services.feeds.poller import FeedPoller, HostLimits

FEED = "https://outlet.example/rss"


def rss(*titles):
    items = "".join(f"<item><title>{t}</title><link>https://outlet.example/{t}</link>"
                    f"<pubDate>Tue, 13 Oct 2026 10:00:00 GMT</pubDate></item>" for t in titles)
    return f"<rss><channel><title>Outlet</title>{items}</channel></rss>".encode()


def advance(monkeypatch, seconds):
    """Moves the poller's clock forward (its own: the event loop's keeps running)."""
    now = poller_module.time.monotonic() + seconds
    monkeypatch.setattr(poller_module, "time", SimpleNamespace(monotonic=lambda: now, time=time.time))


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
    pauses = []
    async with httpx.AsyncClient() as client:
        for _ in range(3):
            await poller.fetch(client, "Outlet", FEED)
            pauses.append(poller.backoff["outlet.example"].until - poller_module.time.monotonic())
            advance(monkeypatch, settings.FEED_BACKOFF_MAX_SECONDS + 1)
    first = settings.FEED_BACKOFF_SECONDS
    assert pauses == pytest.approx([first, first * 2, first * 4], abs=1)


@respx.mock
async def test_a_failing_feed_keeps_its_last_headlines(poller, monkeypatch):
    monkeypatch.setattr(settings, "FETCH_RETRIES", 1)
    respx.get(FEED).mock(side_effect=[httpx.Response(200, content=rss("a")), httpx.ConnectError("down")])
    async with httpx.AsyncClient() as client:
        await poller.fetch(client, "Outlet", FEED)
        poller.feeds[FEED].next_fetch = 0
        assert [h["title"] for h in await poller.fetch(client, "Outlet", FEED)] == ["a"]


def same_host_feeds(n):
    return [SimpleNamespace(name=f"Outlet {i}", url=f"https://outlet.example/{i}/rss") for i in range(n)]


@respx.mock
async def test_requests_to_one_host_are_spaced_out(poller, monkeypatch):
    monkeypatch.setattr(settings, "FEED_HOST_GAP_SECONDS", 0.05)
    started = []

    def answer(request):
        started.append(time.monotonic())
        return httpx.Response(200, content=rss("a"))
    respx.get(url__regex=r"https://outlet\.example/\d/rss").mock(side_effect=answer)
    respx.get("https://other.example/rss").mock(return_value=httpx.Response(200, content=rss("o")))
    other = SimpleNamespace(name="Other", url="https://other.example/rss")
    async with httpx.AsyncClient() as client:
        results = await poller.fetch_all(client, same_host_feeds(3) + [other])
    assert len(results) == 4 and len(started) == 3
    assert all(b - a >= 0.045 for a, b in zip(started, started[1:])), started


@respx.mock
async def test_a_429_stops_the_requests_still_waiting_their_turn(poller, monkeypatch):
    monkeypatch.setattr(settings, "FEED_HOST_GAP_SECONDS", 0.05)
    route = respx.get(url__regex=r"https://outlet\.example/\d/rss").mock(return_value=httpx.Response(429))
    async with httpx.AsyncClient() as client:
        await poller.fetch_all(client, same_host_feeds(3))
    assert route.call_count == 1


@respx.mock
async def test_429s_already_on_their_way_count_as_one(poller):
    respx.get(url__regex=r"https://outlet\.example/\d/rss").mock(return_value=httpx.Response(429))
    async with httpx.AsyncClient() as client:
        await poller.fetch_all(client, same_host_feeds(4))  # No gap: all sent before the first answer
    paused = poller.backoff["outlet.example"]
    assert paused.strikes == 1
    assert paused.until - poller_module.time.monotonic() == pytest.approx(settings.FEED_BACKOFF_SECONDS, abs=1)


@respx.mock
async def test_a_refused_feed_is_not_retried_and_is_asked_again_later(poller, monkeypatch):
    monkeypatch.setattr(settings, "FETCH_RETRIES", 3)
    route = respx.get(FEED).mock(return_value=httpx.Response(403))
    async with httpx.AsyncClient() as client:
        assert await poller.fetch(client, "Outlet", FEED) == []
        assert route.call_count == 1  # A 403 won't change on the second asking
        assert await poller.fetch(client, "Outlet", FEED) == [] and route.call_count == 1  # Not due again yet
    assert poller.feeds[FEED].interval >= 2 * settings.REFRESH_INTERVAL_SECONDS


@respx.mock
async def test_a_page_that_isnt_a_feed_is_not_retried_and_is_asked_again_later(poller, monkeypatch):
    monkeypatch.setattr(settings, "FETCH_RETRIES", 3)
    challenge = b"<html><body><script>if (a && b) document.cookie='x=1'; location.href='/feed?x=1'</script></body></html>"
    route = respx.get(FEED).mock(return_value=httpx.Response(200, content=challenge))  # A bot check's page
    async with httpx.AsyncClient() as client:
        assert await poller.fetch(client, "Outlet", FEED) == []
    assert route.call_count == 1  # The same page again on the second asking
    assert poller.feeds[FEED].interval >= 2 * settings.REFRESH_INTERVAL_SECONDS


@respx.mock
async def test_a_feed_that_keeps_failing_is_asked_less_often(poller, monkeypatch):
    monkeypatch.setattr(settings, "FETCH_RETRIES", 1)
    respx.get(FEED).mock(side_effect=httpx.ConnectError("down"))
    async with httpx.AsyncClient() as client:
        await poller.fetch(client, "Outlet", FEED)
    assert poller.feeds[FEED].interval >= 2 * settings.REFRESH_INTERVAL_SECONDS


def test_an_answer_to_a_request_sent_before_a_pause_does_not_lift_it():
    limits = HostLimits()
    limits.back_off("outlet.example", httpx.Response(429))
    limits.answered("outlet.example")  # A 200 that was already on its way
    assert limits.paused("outlet.example") and limits.backoff["outlet.example"].strikes == 1


@respx.mock
async def test_a_pause_and_its_strikes_outlast_a_restart(mock_redis, monkeypatch):
    respx.get(FEED).mock(return_value=httpx.Response(429))
    async with httpx.AsyncClient() as client:
        await FeedPoller(mock_redis).fetch(client, "Outlet", FEED)  # Paused 10 min, then the refresher restarts
        restarted = FeedPoller(mock_redis)
        await restarted.restore(mock_redis)
        assert restarted.paused("outlet.example")
        assert restarted.backoff["outlet.example"].until - poller_module.time.monotonic() == pytest.approx(
            settings.FEED_BACKOFF_SECONDS, abs=1)
        assert await restarted.fetch(client, "Outlet", FEED) == [] and respx.calls.call_count == 1  # Not asked
        advance(monkeypatch, settings.FEED_BACKOFF_SECONDS + 1)
        await restarted.fetch(client, "Outlet", FEED)  # Refused again: the second strike, twice as long
    assert restarted.backoff["outlet.example"].until - poller_module.time.monotonic() == pytest.approx(
        2 * settings.FEED_BACKOFF_SECONDS, abs=1)


@respx.mock
async def test_a_host_answering_after_its_pause_is_forgotten_across_restarts_too(mock_redis, monkeypatch):
    respx.get(FEED).mock(side_effect=[httpx.Response(429), httpx.Response(200, content=rss("a"))])
    poller = FeedPoller(mock_redis)
    async with httpx.AsyncClient() as client:
        await poller.fetch(client, "Outlet", FEED)
        advance(monkeypatch, settings.FEED_BACKOFF_SECONDS + 1)
        await poller.fetch(client, "Outlet", FEED)
    restarted = FeedPoller(mock_redis)
    await restarted.restore(mock_redis)
    assert "outlet.example" not in restarted.backoff and await mock_redis.keys("host-pause:*") == []


async def test_without_redis_pauses_are_only_remembered_in_memory(monkeypatch):
    class Down:
        def scan_iter(self, *args, **kwargs):
            raise poller_module.aioredis.RedisError("down")
        async def set(self, *args, **kwargs):
            raise poller_module.aioredis.RedisError("down")
    poller = FeedPoller(Down())
    await poller.restore(poller.redis)
    poller.back_off("outlet.example", httpx.Response(429))
    await poller.remember(poller.redis, "outlet.example")
    assert poller.paused("outlet.example")


TOPIC_FEEDS = SimpleNamespace(topic_feeds=[SimpleNamespace(name="Paused Sports", url="https://paused.example/rss"),
                                           SimpleNamespace(name="Outlet Sports", url=FEED)])


@respx.mock
async def test_topic_feeds_skipped_for_a_pause_are_fetched_once_it_ends(poller, monkeypatch):
    paused = respx.get("https://paused.example/rss").mock(side_effect=[httpx.Response(429),
                                                                      httpx.Response(200, content=rss("p"))])
    respx.get(FEED).mock(return_value=httpx.Response(200, content=rss("a")))
    cycle = settings.REFRESH_INTERVAL_SECONDS
    async with httpx.AsyncClient() as client:
        results = await poller.fetch_topic_feeds(client, TOPIC_FEEDS)
        assert results["Paused Sports"] == [] and results["Outlet Sports"]
        advance(monkeypatch, cycle)
        await poller.fetch_topic_feeds(client, TOPIC_FEEDS)  # Still paused: not asked
        assert paused.call_count == 1
        advance(monkeypatch, settings.FEED_BACKOFF_SECONDS)  # The pause is over, the batch isn't due yet
        results = await poller.fetch_topic_feeds(client, TOPIC_FEEDS)
        assert [h["title"] for h in results["Paused Sports"]] == ["p"] and results["Outlet Sports"]
        advance(monkeypatch, cycle)
        await poller.fetch_topic_feeds(client, TOPIC_FEEDS)  # Caught up: it waits for the batch again
    assert paused.call_count == 2


@respx.mock
async def test_a_topic_feed_that_answered_with_an_error_waits_for_the_next_batch(poller, monkeypatch):
    gone = respx.get("https://paused.example/rss").mock(return_value=httpx.Response(404))
    respx.get(FEED).mock(return_value=httpx.Response(200, content=rss("a")))
    async with httpx.AsyncClient() as client:
        await poller.fetch_topic_feeds(client, TOPIC_FEEDS)
        advance(monkeypatch, 2 * settings.REFRESH_INTERVAL_SECONDS)  # Its own interval is over, the batch's isn't
        await poller.fetch_topic_feeds(client, TOPIC_FEEDS)
    assert gone.call_count == 1
