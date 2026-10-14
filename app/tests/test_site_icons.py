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


async def test_icons_opened_directly_cannot_run_anything(client, mock_redis):
    """A publisher's SVG icon could carry a script; opened as a page on our domain, nothing in it may run."""
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    await mock_redis.set(icon_key("svg.example"), json.dumps({"type": "image/svg+xml", "data": base64.b64encode(svg).decode()}))
    response = client.get("/site-icon/svg.example")
    assert response.status_code == 200 and response.headers["content-type"] == "image/svg+xml"
    assert response.headers["content-security-policy"] == "default-src 'none'; sandbox"


@pytest.fixture
def fake_dns(monkeypatch):
    """Every host is public."""
    import services.safe_fetch as safe_fetch

    async def resolve(host, port):
        return ["93.184.216.34"]
    monkeypatch.setattr(safe_fetch, "_resolve", resolve)


def picture(size, fill=(200, 30, 30, 255), bar=(255, 255, 255, 255), mode="RGBA"):
    """A PNG: a filled square with a bar across its middle."""
    import io
    from PIL import Image, ImageDraw
    im = Image.new(mode, (size, size), fill)
    ImageDraw.Draw(im).rectangle((size // 4, size * 3 // 8, size * 3 // 4, size * 5 // 8), fill=bar)
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


@respx.mock
async def test_a_site_missing_at_the_icon_service_gets_its_own_icon_as_pixel_art(mock_redis, fake_dns):
    respx.get(source("pub.example")).mock(return_value=httpx.Response(404))
    respx.get("https://pub.example/").mock(return_value=httpx.Response(200, html=(
        '<html><head><link rel="mask-icon" href="/mask.svg"><link rel="apple-touch-icon" sizes="180x180" '
        'href="/touch.png"><link rel="icon" href="data:,"></head></html>')))
    respx.get("https://pub.example/touch.png").mock(return_value=httpx.Response(200, content=picture(180)))
    respx.get("https://pub.example/favicon.ico").mock(return_value=httpx.Response(404))
    google = respx.get(url__startswith="https://www.google.com/s2/favicons").mock(return_value=httpx.Response(200, content=picture(64)))

    async with httpx.AsyncClient() as client:
        assert await cache_icons(client, mock_redis, ["https://pub.example/story"]) == 1

    icon = json.loads(await mock_redis.get(icon_key("pub.example")))
    svg = base64.b64decode(icon["data"])
    assert icon["type"] == "image/svg+xml" and icon["via"] == "own"
    assert svg.startswith(b"<svg") and b"#c81e1e" in svg and b"#ffffff" in svg  # Its red and white
    assert google.call_count == 0  # Its own was big enough
    assert 0 < await mock_redis.ttl(icon_key("pub.example")) <= settings.ICON_TTL_SECONDS


@respx.mock
async def test_googles_copy_is_used_when_the_site_blocks_us(mock_redis, fake_dns):
    respx.get(source("blocked.example")).mock(return_value=httpx.Response(404))
    respx.get(url__startswith="https://blocked.example/").mock(return_value=httpx.Response(403))
    respx.get(url__startswith="https://www.blocked.example/").mock(return_value=httpx.Response(403))
    respx.get(settings.ICON_FALLBACK_URL.format(host="blocked.example")).mock(return_value=httpx.Response(200, content=picture(64)))
    respx.get(source("nothing.example")).mock(return_value=httpx.Response(404))
    respx.get(url__startswith="https://nothing.example/").mock(return_value=httpx.Response(404))
    respx.get(url__startswith="https://www.nothing.example/").mock(return_value=httpx.Response(404))
    respx.get(settings.ICON_FALLBACK_URL.format(host="nothing.example")).mock(return_value=httpx.Response(404))

    async with httpx.AsyncClient() as client:
        assert await cache_icons(client, mock_redis, ["https://blocked.example/a", "https://nothing.example/b"]) == 1

    assert json.loads(await mock_redis.get(icon_key("blocked.example")))["via"] == "google"
    assert await mock_redis.get(icon_key("nothing.example")) == ""  # No icon anywhere: none, retried after a day


def test_thin_lines_survive_and_only_raster_pictures_are_read():
    import io
    from PIL import Image, ImageDraw
    from services.pixel_icons import picture_size, pixel_art, pixelate
    im = Image.new("RGBA", (192, 192), (0, 0, 0, 0))
    ImageDraw.Draw(im).ellipse((0, 0, 191, 191), outline=(20, 20, 120, 255), width=3)  # A thin ring
    out = io.BytesIO()
    im.save(out, "PNG")
    pixels = pixelate(out.getvalue())
    assert pixels and set(pixels.values()) == {(20, 20, 120)}
    assert {(0, 8), (15, 8), (8, 0), (8, 15)} <= set(pixels)  # The ring is unbroken at its four sides
    assert (8, 8) not in pixels  # And it's still a ring

    assert pixel_art(b'<svg xmlns="http://www.w3.org/2000/svg"/>') is None  # SVG icons aren't decoded
    assert pixel_art(b"not a picture") is None
    assert pixel_art(picture(3000, mode="RGB")) is None  # Too many pixels, however small the file
    assert picture_size(picture(3000, mode="RGB")) == 0 and picture_size(picture(180)) == 180  # So it's never picked
