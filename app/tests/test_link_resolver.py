"""
Tests for link resolution order: the custom resolver is always primary, and the
fallback runs only when the custom resolver's result is still a Google URL.
"""

import json

import httpx
import pytest
import pytest_asyncio
import respx
from fakeredis.aioredis import FakeRedis
from prometheus_client import REGISTRY

from services.link_resolver import decode_with_signature, resolve_link
from services.redirect import RESOLVED_URL_PREFIX

pytestmark = pytest.mark.asyncio

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


def fallbacks(outcome):
    return REGISTRY.get_sample_value("redirect_fallbacks_total", {"outcome": outcome}) or 0


@pytest_asyncio.fixture
async def redis():
    client = FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@respx.mock
async def test_custom_resolver_success_skips_fallback(redis):
    """When the custom resolver finds the publisher URL, the fallback never runs."""
    respx.get(GOOGLE_URL).mock(return_value=httpx.Response(302, headers={"Location": PUBLISHER_URL}))
    respx.get(PUBLISHER_URL).mock(return_value=httpx.Response(200))
    fallback_page = respx.get(ARTICLE_PAGE)
    batch = respx.post(BATCH_URL)
    before = fallbacks("success") + fallbacks("failed")

    async with httpx.AsyncClient() as client:
        assert await resolve_link(client, redis, GOOGLE_URL) == PUBLISHER_URL

    assert not fallback_page.called and not batch.called
    assert fallbacks("success") + fallbacks("failed") == before


@respx.mock
async def test_fallback_used_when_custom_resolver_fails(redis, monkeypatch):
    """Custom resolver returns a Google URL (no c-wiz data-p) -> fallback decodes it and caches the result."""
    respx.get(GOOGLE_URL).mock(return_value=httpx.Response(200, text=PAGE_WITH_SIGNATURE))
    respx.get(ARTICLE_PAGE).mock(return_value=httpx.Response(200, text=PAGE_WITH_SIGNATURE))
    batch = respx.post(BATCH_URL).mock(return_value=httpx.Response(200, text=batch_response(PUBLISHER_URL)))
    before = fallbacks("success")

    async with httpx.AsyncClient() as client:
        assert await resolve_link(client, redis, GOOGLE_URL) == PUBLISHER_URL

    sent = batch.calls.last.request.content.decode()
    assert "Fbv4je" in sent and ARTICLE_ID in sent and "SIG" in sent and "1700000000" in sent
    assert await redis.get(f"{RESOLVED_URL_PREFIX}{GOOGLE_URL}") == PUBLISHER_URL
    assert fallbacks("success") == before + 1


@respx.mock
async def test_both_fail_returns_custom_result(redis, monkeypatch):
    """If the fallback also fails, the custom resolver's result is kept."""
    monkeypatch.setattr("services.redirect.settings.FETCH_RETRIES", 1)
    page = '<html><body>nothing useful</body></html>'
    respx.get(GOOGLE_URL).mock(return_value=httpx.Response(200, text=page))
    respx.get(ARTICLE_PAGE).mock(return_value=httpx.Response(200, text=page))
    before = fallbacks("failed")

    async with httpx.AsyncClient() as client:
        result = await resolve_link(client, redis, GOOGLE_URL)

    assert "news.google.com" in result
    assert fallbacks("failed") == before + 1


@respx.mock
async def test_fallback_error_is_contained(redis, monkeypatch):
    """Network errors in the fallback never propagate; the custom result is returned."""
    monkeypatch.setattr("services.redirect.settings.FETCH_RETRIES", 1)
    respx.get(GOOGLE_URL).mock(side_effect=httpx.ConnectError("down"))

    async with httpx.AsyncClient() as client:
        assert await resolve_link(client, redis, GOOGLE_URL) == GOOGLE_URL


@respx.mock
async def test_non_google_links_are_not_resolved(redis):
    """Direct publisher links (e.g. Hacker News) make no network calls at all."""
    url = "https://publisher.example/direct"
    async with httpx.AsyncClient() as client:
        assert await resolve_link(client, redis, url) == url
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
