"""
Tests for website icons: caching them per domain, and serving them from our domain.
"""

import base64
import json

import httpx
import pytest
import respx

from config import settings
from services.site_icons import cache_icons, icon_host, icon_key

pytestmark = pytest.mark.asyncio

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")


def source(host):
    return settings.ICON_SOURCE_URL.format(host=host)


def test_icon_host():
    assert icon_host("https://www.bbc.co.uk/news/1") == "bbc.co.uk"
    assert icon_host("https://news.ycombinator.com/item?id=1") == "news.ycombinator.com"
    assert icon_host("not a url") is None


@respx.mock
async def test_caches_each_domain_once_and_marks_missing_ones(mock_redis):
    good = respx.get(source("bbc.co.uk")).mock(return_value=httpx.Response(200, content=PNG, headers={"content-type": "image/png"}))
    respx.get(source("unknown.example")).mock(return_value=httpx.Response(404, content=PNG, headers={"content-type": "image/png"}))
    links = ["https://www.bbc.co.uk/a", "https://bbc.co.uk/b", "https://unknown.example/c"]

    async with httpx.AsyncClient() as client:
        assert await cache_icons(client, mock_redis, links) == 1
        assert await cache_icons(client, mock_redis, links) == 0  # Both are known now: no new requests

    assert good.call_count == 1
    assert json.loads(await mock_redis.get(icon_key("bbc.co.uk")))["type"] == "image/png"
    assert await mock_redis.get(icon_key("unknown.example")) == ""
    assert 0 < await mock_redis.ttl(icon_key("unknown.example")) <= settings.ICON_FAILED_TTL_SECONDS


async def test_serves_cached_icons_only(client, mock_redis):
    await mock_redis.set(icon_key("bbc.co.uk"), json.dumps({"type": "image/png", "data": base64.b64encode(PNG).decode()}))
    response = client.get("/site-icon/bbc.co.uk")
    assert response.status_code == 200 and response.content == PNG
    assert response.headers["content-type"] == "image/png" and "max-age=604800" in response.headers["cache-control"]
    missing = client.get("/site-icon/other.example")
    assert missing.status_code == 404 and missing.headers["cache-control"] == "public, max-age=300"
    assert client.get("/site-icon/..%2Fetc").status_code == 404
