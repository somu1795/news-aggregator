"""
Tests for the Google News URL redirect resolution service.

Covers: standard redirects, batchexecute API resolution, Redis URL caching,
parse failure fallbacks, and retry logic.
"""

import json
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock

import httpx
import respx
from fakeredis.aioredis import FakeRedis

from services.redirect import (
    resolve_redirect,
    _parse_batch_execute_response,
    RESOLVED_URL_PREFIX,
    RESOLVED_URL_TTL,
)

pytestmark = pytest.mark.asyncio


# --- Fixtures ---------------------------------------------------------------

@pytest_asyncio.fixture(scope="function")
async def redis():
    client = FakeRedis(decode_responses=True)
    yield client
    await client.close()


@pytest.fixture(scope="function")
def http_client():
    return httpx.AsyncClient()


# --- Test _parse_batch_execute_response -------------------------------------

def test_parse_batch_execute_success():
    """Valid batchexecute response returns the article URL."""
    inner = json.dumps([None, "https://example.com/article"])
    outer = json.dumps([["wQoG1b", "response", inner]])
    raw = ")]}'\n" + outer

    result = _parse_batch_execute_response(raw)
    assert result == "https://example.com/article"


def test_parse_batch_execute_malformed_json():
    """Malformed JSON returns None instead of raising."""
    result = _parse_batch_execute_response("this is not json")
    assert result is None


def test_parse_batch_execute_missing_index():
    """Response with missing nested data returns None."""
    raw = ")]}'\n" + json.dumps([[]])
    result = _parse_batch_execute_response(raw)
    assert result is None


# --- Test resolve_redirect --------------------------------------------------

@respx.mock
async def test_resolve_standard_redirect(redis, http_client):
    """Non-Google URL resolves via standard HTTP redirect following."""
    google_url = "https://news.google.com/rss/articles/test-123"
    final_url = "https://reuters.com/article/real-news"

    respx.get(google_url).mock(
        return_value=httpx.Response(302, headers={"Location": final_url})
    )
    respx.get(final_url).mock(
        return_value=httpx.Response(200)
    )

    result = await resolve_redirect(http_client, redis, google_url)
    assert result == final_url

    # Verify it was cached in Redis
    cached = await redis.get(f"{RESOLVED_URL_PREFIX}{google_url}")
    assert cached == final_url


@respx.mock
async def test_resolve_cached_url(redis, http_client):
    """Previously resolved URL is served from Redis without HTTP calls."""
    original_url = "https://news.google.com/rss/articles/cached-456"
    cached_url = "https://bbc.com/cached-article"

    await redis.setex(f"{RESOLVED_URL_PREFIX}{original_url}", RESOLVED_URL_TTL, cached_url)

    result = await resolve_redirect(http_client, redis, original_url)
    assert result == cached_url
    # No HTTP calls should have been made
    assert not respx.calls


@respx.mock
async def test_resolve_batchexecute(redis, http_client):
    """Google News URL that doesn't redirect resolves via batchexecute API."""
    google_url = "https://news.google.com/rss/articles/batch-789"
    landing_url = "https://news.google.com/articles/batch-789"
    article_url = "https://nytimes.com/real-article"

    # First request returns a Google News page with c-wiz element
    google_html = """
    <html><body>
    <c-wiz data-p='%.@.[\"encodedstuff\",\"more\",1,2,3,4,5,6,7,8]'></c-wiz>
    </body></html>
    """
    respx.get(google_url).mock(
        return_value=httpx.Response(302, headers={"Location": landing_url})
    )
    respx.get(landing_url).mock(
        return_value=httpx.Response(200, text=google_html)
    )

    # batchexecute POST returns the resolved URL
    inner = json.dumps([None, article_url])
    outer = json.dumps([["wQoG1b", "response", inner]])
    batch_response_text = ")]}'\n" + outer

    respx.post("https://news.google.com/_/DotsSplashUi/data/batchexecute").mock(
        return_value=httpx.Response(200, text=batch_response_text)
    )

    result = await resolve_redirect(http_client, redis, google_url)
    assert result == article_url


@respx.mock
async def test_resolve_no_cwiz_element_fallback(redis, http_client):
    """Falls back to Google landing URL when c-wiz element is missing."""
    google_url = "https://news.google.com/rss/articles/nocwiz-101"
    landing_url = "https://news.google.com/articles/nocwiz-101"

    respx.get(google_url).mock(
        return_value=httpx.Response(302, headers={"Location": landing_url})
    )
    respx.get(landing_url).mock(
        return_value=httpx.Response(200, text="<html><body>No c-wiz here</body></html>")
    )

    result = await resolve_redirect(http_client, redis, google_url)
    # Should fall back to the Google landing page URL
    assert result == landing_url


@respx.mock
async def test_resolve_batchexecute_parse_failure(redis, http_client):
    """Falls back to original URL when batchexecute response can't be parsed."""
    google_url = "https://news.google.com/rss/articles/parsefail-202"
    landing_url = "https://news.google.com/articles/parsefail-202"

    google_html = """
    <html><body>
    <c-wiz data-p='%.@.[\"data\",\"more\",1,2,3,4,5,6,7,8]'></c-wiz>
    </body></html>
    """
    respx.get(google_url).mock(
        return_value=httpx.Response(302, headers={"Location": landing_url})
    )
    respx.get(landing_url).mock(
        return_value=httpx.Response(200, text=google_html)
    )

    respx.post("https://news.google.com/_/DotsSplashUi/data/batchexecute").mock(
        return_value=httpx.Response(200, text="garbage response")
    )

    result = await resolve_redirect(http_client, redis, google_url)
    # Should fall back to original URL since parsing failed
    assert result == google_url


@respx.mock
async def test_resolve_retry_on_transient_error(redis, http_client):
    """Transient HTTP errors trigger retries with eventual success."""
    google_url = "https://news.google.com/rss/articles/retry-303"
    final_url = "https://cnn.com/retried-article"

    # First attempt fails, second succeeds with redirect
    respx.get(google_url).mock(side_effect=[
        httpx.ConnectError("Connection refused"),
        httpx.Response(302, headers={"Location": final_url}),
    ])
    respx.get(final_url).mock(
        return_value=httpx.Response(200)
    )

    result = await resolve_redirect(http_client, redis, google_url)
    assert result == final_url
    assert len(respx.calls) == 3


@respx.mock
async def test_resolve_all_retries_exhausted(redis, http_client):
    """Falls back to original URL when all retry attempts fail."""
    google_url = "https://news.google.com/rss/articles/allfail-404"

    respx.get(google_url).mock(side_effect=httpx.ConnectError("Connection refused"))

    result = await resolve_redirect(http_client, redis, google_url)
    # Should return the original URL as final fallback
    assert result == google_url


async def test_resolve_redis_cache_failure_non_fatal(http_client):
    """Redis errors during URL cache lookup are non-fatal."""
    broken_redis = AsyncMock()
    broken_redis.get = AsyncMock(side_effect=ConnectionError("Redis down"))
    broken_redis.setex = AsyncMock(side_effect=ConnectionError("Redis down"))

    url = "https://example.com/non-google-url"

    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(200, request=httpx.Request("GET", url)))
        result = await resolve_redirect(http_client, broken_redis, url)

    # Should still return a result even though Redis is broken
    assert result == url
