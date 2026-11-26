"""Tests for polite feed polling: conditional requests, per-feed intervals, host backoff."""

import json
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
        assert route.call_count == 2  # Once as each client (below); a 403 won't change on a third asking
        assert await poller.fetch(client, "Outlet", FEED) == [] and route.call_count == 2  # Not due again yet
    assert poller.feeds[FEED].interval >= 2 * settings.REFRESH_INTERVAL_SECONDS


def as_browser(request):
    return request.headers["User-Agent"] == settings.BROWSER_USER_AGENT


def refusing_the_browser(refusal):
    """A feed that refuses a client calling itself Chrome (the Guardian since Nov 23, 2026; Hacker News: 419)."""
    def answer(request):
        if as_browser(request):
            if isinstance(refusal, Exception):
                raise refusal
            return httpx.Response(refusal)
        return httpx.Response(200, content=rss("a"))
    return answer


# Over HTTP/2 the Guardian's refusal reached httpx as a protocol error, retried 3 times as a network error
@pytest.mark.parametrize("refusal", [403, 419, httpx.LocalProtocolError("Invalid input ConnectionInputs.RECV_DATA in state ConnectionState.CLOSED")])
@respx.mock
async def test_a_feed_refusing_the_browser_identity_is_asked_at_once_as_a_plain_client(poller, monkeypatch, refusal):
    monkeypatch.setattr(settings, "FETCH_RETRIES", 3)
    route = respx.get(FEED).mock(side_effect=refusing_the_browser(refusal))
    async with httpx.AsyncClient() as client:
        assert [h["title"] for h in await poller.fetch(client, "Outlet", FEED)] == ["a"]
        assert route.call_count == 2 and as_browser(route.calls[0].request) and not as_browser(route.calls[1].request)
        poller.feeds[FEED].next_fetch = 0  # Due again: asked as the client that got an answer, first
        assert [h["title"] for h in await poller.fetch(client, "Outlet", FEED)] == ["a"]
    assert route.call_count == 3 and not as_browser(route.calls[2].request)


@respx.mock
async def test_a_feed_refusing_the_plain_client_goes_back_to_the_browser_identity(poller):
    poller.feeds[FEED] = poller_module._FeedState(plain=True)
    route = respx.get(FEED).mock(side_effect=lambda request: httpx.Response(200, content=rss("a")) if as_browser(request)
                                 else httpx.Response(403))  # Balkan Insight, Colombia One
    async with httpx.AsyncClient() as client:
        assert [h["title"] for h in await poller.fetch(client, "Outlet", FEED)] == ["a"]
    assert route.call_count == 2 and as_browser(route.calls[1].request) and not poller.feeds[FEED].plain


@pytest.mark.parametrize("status", [404, 410])
@respx.mock
async def test_a_missing_feed_is_not_asked_again_as_another_client(poller, status):
    route = respx.get(FEED).mock(return_value=httpx.Response(status))  # Kyodo's feed, gone in its redesign
    async with httpx.AsyncClient() as client:
        assert await poller.fetch(client, "Outlet", FEED) == []
    assert route.call_count == 1 and not poller.feeds[FEED].plain


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


# A restarted refresher takes up each feed where it was: a restart left every feed due at once, and ~100
# Google feeds asked within minutes drew a 429 and a 10-minute pause on every restart
@respx.mock
async def test_a_restart_takes_up_each_feeds_schedule_headlines_and_validators(mock_redis, monkeypatch):
    route = respx.get(FEED).mock(side_effect=[
        httpx.Response(200, content=rss("a", "b"), headers={"ETag": '"v1"'}),
        httpx.Response(304),
        httpx.Response(304),
    ])
    cycle = settings.REFRESH_INTERVAL_SECONDS
    before = FeedPoller(mock_redis)
    async with httpx.AsyncClient() as client:
        await before.fetch_all(client, [SimpleNamespace(name="Outlet", url=FEED)])
        advance(monkeypatch, cycle)
        await before.fetch_all(client, [SimpleNamespace(name="Outlet", url=FEED)])  # Unchanged: next asked in 2 cycles
        restarted = FeedPoller(mock_redis)
        await restarted.restore(mock_redis)
        results = await restarted.fetch_all(client, [SimpleNamespace(name="Outlet", url=FEED)])
        assert [h["title"] for h in results["Outlet"]] == ["a", "b"] and route.call_count == 2  # Not due: not asked
        advance(monkeypatch, 2 * cycle)
        results = await restarted.fetch_all(client, [SimpleNamespace(name="Outlet", url=FEED)])
    assert route.call_count == 3 and route.calls[2].request.headers["If-None-Match"] == '"v1"'
    assert [h["title"] for h in results["Outlet"]] == ["a", "b"]


@respx.mock
async def test_a_restart_keeps_asking_a_feed_as_the_client_it_answered(mock_redis, monkeypatch):
    route = respx.get(FEED).mock(side_effect=refusing_the_browser(403))
    async with httpx.AsyncClient() as client:
        await FeedPoller(mock_redis).fetch_all(client, [SimpleNamespace(name="Outlet", url=FEED)])
        restarted = FeedPoller(mock_redis)
        await restarted.restore(mock_redis)
        advance(monkeypatch, settings.FEED_MAX_INTERVAL_SECONDS)
        await restarted.fetch_all(client, [SimpleNamespace(name="Outlet", url=FEED)])
    assert route.call_count == 3 and not as_browser(route.calls[2].request)


@respx.mock
async def test_a_feeds_saved_headlines_leave_out_the_article_texts(mock_redis):
    feed = (b"<rss><channel><title>Outlet</title><item><title>a</title><link>https://outlet.example/a</link>"
            b"<description>" + b"word " * 500 + b"</description></item></channel></rss>")
    respx.get(FEED).mock(return_value=httpx.Response(200, content=feed))
    async with httpx.AsyncClient() as client:
        results = await FeedPoller(mock_redis).fetch_all(client, [SimpleNamespace(name="Outlet", url=FEED)])
    assert results["Outlet"][0]["feed_text"]  # Still there for this cycle (the summarizer's copy is stored apart)
    saved = [await mock_redis.get(key) for key in await mock_redis.keys("feed-state:*")]
    assert len(saved) == 1 and b"word word" not in (saved[0] if isinstance(saved[0], bytes) else saved[0].encode())


@respx.mock
async def test_a_restart_keeps_the_topic_batchs_time_and_its_headlines(mock_redis):
    paused = respx.get("https://paused.example/rss").mock(return_value=httpx.Response(200, content=rss("p")))
    outlet = respx.get(FEED).mock(return_value=httpx.Response(200, content=rss("a")))
    async with httpx.AsyncClient() as client:
        await FeedPoller(mock_redis).fetch_topic_feeds(client, TOPIC_FEEDS)
        restarted = FeedPoller(mock_redis)
        await restarted.restore(mock_redis)
        results = await restarted.fetch_topic_feeds(client, TOPIC_FEEDS)  # The batch isn't due: nothing asked
    assert paused.call_count == outlet.call_count == 1
    assert [h["title"] for h in results["Paused Sports"]] == ["p"] and [h["title"] for h in results["Outlet Sports"]] == ["a"]


async def test_without_redis_feeds_start_afresh_as_before():
    class Down:
        def scan_iter(self, *args, **kwargs):
            raise poller_module.aioredis.RedisError("down")
        async def get(self, *args, **kwargs):
            raise poller_module.aioredis.RedisError("down")
        def pipeline(self, *args, **kwargs):
            raise poller_module.aioredis.RedisError("down")
    poller = FeedPoller(Down())
    await poller.restore(poller.redis)  # Nothing restored, nothing raised
    poller.dirty.add(FEED)
    await poller.save_feeds()
    assert poller.feeds == {}


# --- A feed that stops working is reported (Hacker News refused us for a week before anyone noticed, Nov 2026) ---

ALERT = "http://alerts.lan/webhook"
DAY = 86400


def later(monkeypatch, seconds):
    """Moves both of the poller's clocks forward: its schedule's and the wall clock its health uses."""
    mono, wall = poller_module.time.monotonic() + seconds, poller_module.time.time() + seconds
    monkeypatch.setattr(poller_module, "time", SimpleNamespace(monotonic=lambda: mono, time=lambda: wall))


async def cycle(poller, client):
    for state in poller.feeds.values():
        state.next_fetch = 0  # Due
    return await poller.fetch_all(client, [SimpleNamespace(name="Outlet", url=FEED)])


def alerts_sent(route):
    return [json.loads(call.request.content) for call in route.calls]


@respx.mock
async def test_a_feed_with_no_usable_answer_for_a_day_is_reported_once(poller, monkeypatch):
    monkeypatch.setattr(settings, "FEED_ALERT_URL", ALERT)
    alert = respx.post(ALERT).mock(return_value=httpx.Response(200))
    respx.get(FEED).mock(return_value=httpx.Response(404))  # Kyodo's feed, gone in its redesign
    async with httpx.AsyncClient() as client:
        await cycle(poller, client)
        later(monkeypatch, DAY - 60)
        await cycle(poller, client)
        assert alert.call_count == 0  # Not a day yet
        later(monkeypatch, 120)
        await cycle(poller, client)
        later(monkeypatch, 3600)
        await cycle(poller, client)
    sent = alerts_sent(alert)
    assert len(sent) == 1 and sent[0]["title"] == "Feed not working: Outlet"
    assert "404" in sent[0]["message"]


@respx.mock
async def test_a_reported_feed_that_answers_again_is_reported_working(poller, monkeypatch):
    monkeypatch.setattr(settings, "FEED_ALERT_URL", ALERT)
    monkeypatch.setattr(settings, "FETCH_RETRIES", 1)
    alert = respx.post(ALERT).mock(return_value=httpx.Response(200))
    feed = respx.get(FEED).mock(return_value=httpx.Response(500))
    async with httpx.AsyncClient() as client:
        await cycle(poller, client)
        later(monkeypatch, DAY + 60)
        await cycle(poller, client)
        feed.mock(return_value=httpx.Response(200, content=rss("a")))
        later(monkeypatch, 600)
        await cycle(poller, client)
    assert [a["title"] for a in alerts_sent(alert)] == ["Feed not working: Outlet", "Feed working again: Outlet"]


@respx.mock
async def test_a_feed_with_no_headlines_counts_as_not_working(poller, monkeypatch):
    monkeypatch.setattr(settings, "FEED_ALERT_URL", ALERT)
    alert = respx.post(ALERT).mock(return_value=httpx.Response(200))
    respx.get(FEED).mock(return_value=httpx.Response(200, content=rss()))
    async with httpx.AsyncClient() as client:
        await cycle(poller, client)
        later(monkeypatch, DAY + 60)
        await cycle(poller, client)
    assert [a["title"] for a in alerts_sent(alert)] == ["Feed not working: Outlet"]


@respx.mock
async def test_a_quiet_feed_answering_304_is_working(poller, monkeypatch):
    monkeypatch.setattr(settings, "FEED_ALERT_URL", ALERT)
    alert = respx.post(ALERT).mock(return_value=httpx.Response(200))
    respx.get(FEED).mock(side_effect=[httpx.Response(200, content=rss("a"), headers={"ETag": '"v1"'})] + [httpx.Response(304)] * 10)
    async with httpx.AsyncClient() as client:
        for _ in range(5):
            await cycle(poller, client)
            later(monkeypatch, DAY / 2)
    assert alert.call_count == 0


@respx.mock
async def test_without_an_alert_address_it_is_only_logged(poller, monkeypatch):
    monkeypatch.setattr(settings, "FEED_ALERT_URL", "")
    respx.get(FEED).mock(return_value=httpx.Response(404))  # respx fails any other request
    async with httpx.AsyncClient() as client:
        await cycle(poller, client)
        later(monkeypatch, DAY + 60)
        await cycle(poller, client)
    assert poller.feeds[FEED].reported


@respx.mock
async def test_an_alert_that_cant_be_sent_never_stops_a_refresh(poller, monkeypatch):
    monkeypatch.setattr(settings, "FEED_ALERT_URL", ALERT)
    respx.post(ALERT).mock(side_effect=httpx.ConnectError("Home Assistant is restarting"))
    respx.get(FEED).mock(return_value=httpx.Response(404))
    async with httpx.AsyncClient() as client:
        await cycle(poller, client)
        later(monkeypatch, DAY + 60)
        results = await cycle(poller, client)
    assert results == {"Outlet": []}


@respx.mock
async def test_a_restart_neither_reports_a_feed_again_nor_forgets_how_long_it_failed(mock_redis, monkeypatch):
    monkeypatch.setattr(settings, "FEED_ALERT_URL", ALERT)
    alert = respx.post(ALERT).mock(return_value=httpx.Response(200))
    respx.get(FEED).mock(return_value=httpx.Response(404))
    async with httpx.AsyncClient() as client:
        before = FeedPoller(mock_redis)
        await cycle(before, client)
        later(monkeypatch, DAY - 600)
        await cycle(before, client)
        restarted = FeedPoller(mock_redis)
        await restarted.restore(mock_redis)
        later(monkeypatch, 1200)
        await cycle(restarted, client)  # A day since its last usable answer, across the restart
        again = FeedPoller(mock_redis)
        await again.restore(mock_redis)
        later(monkeypatch, 600)
        await cycle(again, client)
    assert alert.call_count == 1
