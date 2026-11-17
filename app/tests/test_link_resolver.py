"""
Tests for link resolution order: the custom resolver is always primary, and the
fallback runs only when the custom resolver's result is still a Google URL.
"""

import json

import httpx
import respx
from structlog.testing import capture_logs

from services.links.google_news import RESOLVED_URL_PREFIX, _is_unresolved, decode_with_signature, resolve_link

ARTICLE_ID = "CBMiTestArticle123"
GOOGLE_URL = f"https://news.google.com/rss/articles/{ARTICLE_ID}?oc=5"
ARTICLE_PAGE = f"https://news.google.com/rss/articles/{ARTICLE_ID}"
BATCH_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
PUBLISHER_URL = "https://publisher.example/story"

# Article page without the c-wiz[data-p] payload the custom resolver needs,
# but with the signature/timestamp the fallback uses.
PAGE_WITH_SIGNATURE = '<html><body><c-wiz><div jscontroller="x" data-n-a-sg="SIG" data-n-a-ts="1700000000"></div></c-wiz></body></html>'


def batch_response(url):
    return ")]}'\n\n" + json.dumps([["wrb.fr", "Fbv4je", json.dumps(["garturlres", url, 1]), None, None, None, "generic"]])


@respx.mock
async def test_custom_resolver_success_skips_fallback(mock_redis):
    """When the custom resolver finds the publisher URL, the fallback never runs."""
    respx.get(GOOGLE_URL).mock(return_value=httpx.Response(302, headers={"Location": PUBLISHER_URL}))
    respx.get(PUBLISHER_URL).mock(return_value=httpx.Response(200))
    fallback_page = respx.get(ARTICLE_PAGE)
    batch = respx.post(BATCH_URL)

    async with httpx.AsyncClient() as client:
        assert await resolve_link(client, mock_redis, GOOGLE_URL) == PUBLISHER_URL

    assert not fallback_page.called and not batch.called


@respx.mock
async def test_fallback_used_when_custom_resolver_fails(mock_redis, monkeypatch):
    """Custom resolver returns a Google URL (no c-wiz data-p) -> fallback decodes it and caches the result."""
    respx.get(GOOGLE_URL).mock(return_value=httpx.Response(200, text=PAGE_WITH_SIGNATURE))
    respx.get(ARTICLE_PAGE).mock(return_value=httpx.Response(200, text=PAGE_WITH_SIGNATURE))
    batch = respx.post(BATCH_URL).mock(return_value=httpx.Response(200, text=batch_response(PUBLISHER_URL)))

    async with httpx.AsyncClient() as client:
        assert await resolve_link(client, mock_redis, GOOGLE_URL) == PUBLISHER_URL

    sent = batch.calls.last.request.content.decode()
    assert "Fbv4je" in sent and ARTICLE_ID in sent and "SIG" in sent and "1700000000" in sent
    assert await mock_redis.get(f"{RESOLVED_URL_PREFIX}{GOOGLE_URL}") == PUBLISHER_URL


@respx.mock
async def test_both_fail_returns_custom_result(mock_redis, monkeypatch):
    """If the fallback also fails, the custom resolver's result is kept."""
    monkeypatch.setattr("services.links.google_news.settings.FETCH_RETRIES", 1)
    page = '<html><body>nothing useful</body></html>'
    respx.get(GOOGLE_URL).mock(return_value=httpx.Response(200, text=page))
    respx.get(ARTICLE_PAGE).mock(return_value=httpx.Response(200, text=page))

    async with httpx.AsyncClient() as client:
        result = await resolve_link(client, mock_redis, GOOGLE_URL)

    assert "news.google.com" in result


@respx.mock
async def test_fallback_error_is_contained(mock_redis, monkeypatch):
    """Network errors in the fallback never propagate; the custom result is returned."""
    monkeypatch.setattr("services.links.google_news.settings.FETCH_RETRIES", 1)
    respx.get(GOOGLE_URL).mock(side_effect=httpx.ConnectError("down"))

    async with httpx.AsyncClient() as client:
        assert await resolve_link(client, mock_redis, GOOGLE_URL) == GOOGLE_URL


@respx.mock
async def test_non_google_links_are_not_resolved(mock_redis):
    """Direct publisher links (e.g. Hacker News) make no network calls at all."""
    url = "https://publisher.example/direct"
    async with httpx.AsyncClient() as client:
        assert await resolve_link(client, mock_redis, url) == url
    assert not respx.calls


@respx.mock
async def test_decoder_rejects_google_or_malformed_results():
    respx.get(ARTICLE_PAGE).mock(return_value=httpx.Response(200, text=PAGE_WITH_SIGNATURE))
    respx.post(BATCH_URL).mock(side_effect=[
        httpx.Response(200, text=batch_response("https://news.google.com/still-google")),
        httpx.Response(200, text="garbage"),
    ])
    async with httpx.AsyncClient() as client:
        assert await decode_with_signature(client, GOOGLE_URL) is None
        assert await decode_with_signature(client, GOOGLE_URL) is None
        assert await decode_with_signature(client, "https://news.google.com/topics/abc") is None


def test_only_google_news_hosts_count_as_unresolved():
    assert _is_unresolved("https://news.google.com/rss/articles/abc")
    assert not _is_unresolved("https://evil.example/?u=news.google.com")
    assert not _is_unresolved("https://news.google.com.evil.example/a")


@respx.mock
async def test_google_asking_to_slow_down_pauses_resolution_without_retries(mock_redis, monkeypatch):
    """A 429 isn't retried; the link stays a Google one (and uncached) until a later refresh; nothing more is sent meanwhile."""
    monkeypatch.setattr("services.links.google_news.settings.FETCH_RETRIES", 3)
    page = respx.get(GOOGLE_URL).mock(return_value=httpx.Response(429))
    fallback_page = respx.get(ARTICLE_PAGE)
    other = "https://news.google.com/rss/articles/CBMiOther?oc=5"
    other_page = respx.get(other)

    async with httpx.AsyncClient() as client:
        with capture_logs() as logs:
            assert await resolve_link(client, mock_redis, GOOGLE_URL) == GOOGLE_URL
            assert await resolve_link(client, mock_redis, other) == other

    assert page.call_count == 1 and not fallback_page.called and not other_page.called
    assert [e["event"] for e in logs if e["log_level"] == "warning"] == [
        "Google asked us to slow down, pausing link resolution"]  # Once, not once per link left
    assert await mock_redis.get(f"{RESOLVED_URL_PREFIX}{GOOGLE_URL}") is None


@respx.mock
async def test_googles_pause_on_link_resolution_outlasts_a_restart(mock_redis, monkeypatch):
    from services.feeds.poller import HostLimits
    from services.links import google_news
    page = respx.get(GOOGLE_URL).mock(return_value=httpx.Response(429))
    async with httpx.AsyncClient() as client:
        await resolve_link(client, mock_redis, GOOGLE_URL)
        monkeypatch.setattr(google_news, "_GOOGLE", HostLimits("links"))  # The refresher restarts
        await google_news.restore_pause(mock_redis)
        assert await resolve_link(client, mock_redis, GOOGLE_URL) == GOOGLE_URL
    assert page.call_count == 1  # Still paused: not asked again
    assert await mock_redis.keys("host-pause:feeds:*") == []  # The feeds' pauses are their own


@respx.mock
async def test_a_publisher_refusing_the_redirect_still_gives_its_url(mock_redis):
    """Google redirected to the publisher, whose 403 (to a bot) doesn't matter: its URL is the answer."""
    respx.get(GOOGLE_URL).mock(return_value=httpx.Response(302, headers={"Location": PUBLISHER_URL}))
    publisher = respx.get(PUBLISHER_URL).mock(return_value=httpx.Response(403))
    fallback_page = respx.get(ARTICLE_PAGE)

    async with httpx.AsyncClient() as client:
        assert await resolve_link(client, mock_redis, GOOGLE_URL) == PUBLISHER_URL

    assert publisher.call_count == 1 and not fallback_page.called


@respx.mock
async def test_requests_to_google_are_spaced_out(mock_redis, monkeypatch):
    import time
    from services.links import resolve_links
    monkeypatch.setattr("services.links.google_news.settings.LINK_RESOLVE_GAP_SECONDS", 0.05)
    started = []

    def answer(request):
        started.append(time.monotonic())
        return httpx.Response(302, headers={"Location": f"{PUBLISHER_URL}/{request.url.path.rsplit('/', 1)[-1]}"})
    respx.get(url__regex=r"https://news\.google\.com/rss/articles/.*").mock(side_effect=answer)
    respx.get(url__regex=rf"{PUBLISHER_URL}/.*").mock(return_value=httpx.Response(200))
    headlines = [{"link": f"https://news.google.com/rss/articles/CBMi{i}"} for i in range(4)]

    async with httpx.AsyncClient() as client:
        await resolve_links(client, mock_redis, headlines)

    assert [h["link"] for h in headlines] == [f"{PUBLISHER_URL}/CBMi{i}" for i in range(4)]
    assert len(started) == 4 and all(b - a >= 0.045 for a, b in zip(started, started[1:])), started
