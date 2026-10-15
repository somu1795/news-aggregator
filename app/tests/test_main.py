import json
import re
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import redis.asyncio as aioredis
from starlette.requests import Request

from deps import get_real_ip, limiter
from main import app
from config import ALL_VIEW, CATALOG, settings, snapshot_key, topic_view

# Using a fixed time for reproducible tests
FIXED_TIME_UNIX = 1672531200 # 2023-01-01 00:00:00

# Use pytest-asyncio for all async tests in this module
pytestmark = pytest.mark.asyncio


async def publish_snapshot(redis_client, title="Cached News", fetched_at=None, view=ALL_VIEW):
    """Writes a snapshot to Redis the same way the refresher does."""
    fetched_at = fetched_at or time.time()
    snapshot = {
        "region": view,
        "data": [{
            "title": title,
            "link": "http://cache.com",
            "source": "BBC",
            "published": FIXED_TIME_UNIX,
            "last_updated": fetched_at,
        }],
        "fetched_at": fetched_at,
        "expires_at": fetched_at + settings.REFRESH_INTERVAL_SECONDS,
    }
    await redis_client.set(snapshot_key(view), json.dumps(snapshot))


async def publish_view(redis_client, view, *links, fetched_at=None):
    """A snapshot of a view whose items are these links (each titled after its link)."""
    fetched_at = fetched_at or time.time()
    data = [{"title": link, "link": link, "source": "BBC", "published": FIXED_TIME_UNIX, "last_updated": fetched_at}
            for link in links]
    await redis_client.set(snapshot_key(view), json.dumps({
        "region": view, "data": data, "fetched_at": fetched_at, "expires_at": fetched_at + settings.REFRESH_INTERVAL_SECONDS}))


# --- Test Endpoints ---

async def test_health_check_success(client, mock_redis):
    """Tests that the /health endpoint returns 200 and reports headline age."""
    await publish_snapshot(mock_redis, fetched_at=time.time() - 30)
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["redis"] is True
    assert data["version"] == settings.APP_VERSION
    assert 29 <= data["headlines"]["age_seconds"] <= 31
    assert data["headlines"]["stale"] is False

def test_health_check_no_headlines_is_stale_but_healthy(client):
    """Missing headlines are reported but don't fail the health probe."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["headlines"] == {"age_seconds": None, "stale": True}

async def test_health_check_redis_failure_with_snapshot_is_degraded(client, mock_redis):
    """Redis down but a snapshot in memory: still serving, so the probe passes."""
    await publish_snapshot(mock_redis)
    client.get("/api/headlines")
    mock_redis.ping = AsyncMock(side_effect=ConnectionError("Redis is down"))
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["redis"] is False

def test_health_check_redis_failure(client, mock_redis):
    """Tests that the /health endpoint returns 503 when Redis is down and nothing can be served."""
    mock_redis.ping = AsyncMock(side_effect=ConnectionError("Redis is down"))
    response = client.get("/health")
    assert response.status_code == 503
    data = response.json()
    assert data["status"] == "unhealthy"
    assert data["redis"] is False
    assert data["version"] == settings.APP_VERSION

def test_root_endpoint_html(client):
    """The main page (retro design) is served to browsers, with every region as a tab."""
    response = client.get("/", headers={"Accept": "text/html"})
    assert response.status_code == 200
    assert "text/html" in response.headers['content-type']
    assert "NEWS.TXT" in response.text
    views = json.loads(response.text.split('<script id="views-data" type="application/json">')[1].split("</script>")[0])
    assert [v["id"] for v in views] == CATALOG.view_ids
    assert views[0] == {"id": "all", "label": "All"}
    topics = json.loads(response.text.split('<script id="topics-data" type="application/json">')[1].split("</script>")[0])
    assert [t["id"] for t in topics] == ["everything"] + [t.id for t in CATALOG.topics]

@pytest.mark.parametrize("path", ["/retro", "/archive"])
def test_old_urls_redirect_home(client, path):
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 301
    assert response.headers["location"] == "/"

def test_page_has_a_content_security_policy(client):
    response = client.get("/", headers={"Accept": "text/html"})
    csp = response.headers["content-security-policy"]
    nonce = csp.split("script-src 'nonce-")[1].split("'")[0]
    assert re.search(rf'<link rel="stylesheet" href="/static/retro\.css\?v=\w+" nonce="{nonce}">', response.text)
    assert re.search(rf'<script src="/static/retro\.js\?v=\w+" nonce="{nonce}" defer></script>', response.text)
    assert "<style" not in response.text and "<script>" not in response.text  # Nothing inline but data
    assert "unsafe-inline" not in csp and "frame-ancestors 'none'" in csp
    assert client.get("/", headers={"Accept": "text/html"}).headers["content-security-policy"] != csp  # Fresh per response

@pytest.mark.parametrize("name, content_type", [("retro.css", "text/css"), ("retro.js", "text/javascript")])
def test_page_assets_are_kept_by_content_hash(client, name, content_type):
    """The page links to its stylesheet and script by content hash, so they can be cached for a year."""
    url = re.search(rf'"(/static/{re.escape(name)}\?v=\w+)"', client.get("/", headers={"Accept": "text/html"}).text).group(1)
    response = client.get(url)
    assert response.status_code == 200 and response.headers["content-type"].startswith(content_type)
    assert response.content == (Path("static") / name).read_bytes()
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    # Any other version (an old page's link, or none) is the current file, not to be kept
    assert client.get(f"/static/{name}?v=0123456789ab").headers["cache-control"] == "no-cache"
    assert client.get(f"/static/{name}").headers["cache-control"] == "no-cache"
    assert client.get("/static/missing.js").status_code == 404

@pytest.mark.parametrize("path", ["/", "/api/headlines", "/api/search-index", "/health"])
def test_head_requests(client, path):
    assert client.head(path).status_code != 405

def test_paywall_bypass_links(client):
    """archive.today links go straight to the newest snapshot; the defunct 12ft.io is gone."""
    page = client.get("/", headers={"Accept": "text/html"}).text
    script = client.get(re.search(r'<script src="([^"]+)"', page).group(1)).text
    assert "https://archive.ph/newest/" in script
    assert "12ft.io/" not in script

def test_root_endpoint_json(client):
    """Tests that the root endpoint serves JSON to API clients."""
    response = client.get("/", headers={"Accept": "application/json"})
    assert response.status_code == 200
    assert "application/json" in response.headers['content-type']
    data = response.json()
    assert data["message"] == "News Aggregator API is running."
    assert data["version"] == settings.APP_VERSION

async def test_get_headlines_serves_snapshot(client, mock_redis):
    """Tests that /api/headlines serves the snapshot published by the refresher."""
    await publish_snapshot(mock_redis)

    response = client.get("/api/headlines")

    assert response.status_code == 200
    assert response.headers["cache-control"].startswith("public, max-age=")
    max_age = int(response.headers["cache-control"].split("=")[1])
    assert 0 < max_age <= settings.REFRESH_INTERVAL_SECONDS
    edge = response.headers["cdn-cache-control"]  # Cloudflare keeps it at least a minute, and serves stale on errors
    assert f"max-age={max(max_age, 60)}," in edge and "stale-if-error=86400" in edge

    data = response.json()
    assert len(data["data"]) == 1
    assert data["data"][0]["title"] == "Cached News"
    assert "fetched_at" in data and "expires_at" in data

async def test_get_headlines_by_region(client, mock_redis):
    """Each region is its own snapshot; "all" is the default."""
    region = CATALOG.regions[0].id
    await publish_snapshot(mock_redis, title="Everywhere")
    await publish_snapshot(mock_redis, title="Regional", view=region)

    assert client.get("/api/headlines").json()["data"][0]["title"] == "Everywhere"
    regional = client.get(f"/api/headlines?region={region}").json()
    assert regional["region"] == region and regional["data"][0]["title"] == "Regional"

async def test_get_headlines_by_topic(client, mock_redis):
    """Each region's topic is its own snapshot; no topic means the region's main view."""
    region, topic = CATALOG.regions[0].id, CATALOG.topics[0].id
    await publish_snapshot(mock_redis, title="Regional", view=region)
    await publish_snapshot(mock_redis, title="Regional topic", view=topic_view(region, topic))
    await publish_snapshot(mock_redis, title="Everywhere topic", view=topic_view(ALL_VIEW, topic))

    assert client.get(f"/api/headlines?region={region}").json()["data"][0]["title"] == "Regional"
    assert client.get(f"/api/headlines?region={region}&topic={topic}").json()["data"][0]["title"] == "Regional topic"
    assert client.get(f"/api/headlines?topic={topic}").json()["data"][0]["title"] == "Everywhere topic"

async def test_search_index_has_each_link_once_with_its_views(client, mock_redis):
    """Every view's headlines in one response, for the page's search: each link once, with the views it's in."""
    region, topic = CATALOG.regions[0].id, CATALOG.topics[0].id
    now = time.time()
    await publish_view(mock_redis, topic_view(region, topic), "https://a.example/shared", fetched_at=now - 30)
    await publish_view(mock_redis, region, "https://a.example/regional", "https://a.example/shared", fetched_at=now - 60)
    await publish_view(mock_redis, ALL_VIEW, "https://a.example/shared", fetched_at=now - 60)

    response = client.get("/api/search-index")

    assert response.status_code == 200
    body = response.json()
    assert [(item["link"], item["views"]) for item in body["items"]] == [
        ("https://a.example/shared", [ALL_VIEW, region, topic_view(region, topic)]),  # Tab order: a region, then its topics
        ("https://a.example/regional", [region]),
    ]
    assert body["items"][1]["source"] == "BBC"  # The whole item, as in /api/headlines
    # Fresh until the soonest view is due to be refreshed
    assert body["fetched_at"] == now - 30 and body["expires_at"] == now - 60 + settings.REFRESH_INTERVAL_SECONDS
    max_age = int(response.headers["cache-control"].split("=")[1])
    assert 0 < max_age <= settings.REFRESH_INTERVAL_SECONDS - 60
    assert f"max-age={max(max_age, 60)}," in response.headers["cdn-cache-control"]

def test_search_index_before_first_refresh(client):
    response = client.get("/api/search-index")
    assert response.status_code == 503 and response.headers["retry-after"] == "10"

async def test_search_index_is_rebuilt_only_when_a_view_changes(mock_redis, monkeypatch):
    from services.headlines_cache import HeadlinesCache

    views = [ALL_VIEW, CATALOG.regions[0].id]
    await publish_view(mock_redis, ALL_VIEW, "https://a.example/1")
    cache = HeadlinesCache(mock_redis, views)
    first = await cache.search_index(views)
    assert await cache.search_index(views) is first

    await publish_view(mock_redis, views[1], "https://a.example/2")  # A view's first snapshot
    second = await cache.search_index(views)
    assert second is not first and '"https://a.example/2"' in second.body

    monkeypatch.setattr(settings, "LOCAL_CACHE_SECONDS", 0)
    await publish_view(mock_redis, ALL_VIEW, "https://a.example/3")  # A newer snapshot of a view
    third = await cache.search_index(views)
    assert '"https://a.example/3"' in third.body and '"https://a.example/1"' not in third.body
    assert await cache.search_index(views) is third

def test_get_headlines_unknown_topic(client):
    response = client.get("/api/headlines?topic=gossip")
    assert response.status_code == 404
    assert "Unknown topic" in response.json()["detail"]

def test_get_headlines_unknown_region(client):
    response = client.get("/api/headlines?region=atlantis")
    assert response.status_code == 404
    assert "asia" in response.json()["detail"]

def test_get_headlines_before_first_refresh(client):
    """Tests that a 503 with Retry-After is returned until a snapshot exists."""
    response = client.get("/api/headlines")
    assert response.status_code == 503
    assert response.headers["retry-after"] == "10"
    assert "detail" in response.json()

async def test_get_headlines_does_not_hit_redis_per_request(client, mock_redis):
    """Repeated requests within LOCAL_CACHE_SECONDS are served from memory."""
    await publish_snapshot(mock_redis)
    client.get("/api/headlines")

    mock_redis.get = AsyncMock(side_effect=AssertionError("Redis should not be read"))
    for _ in range(5):
        assert client.get("/api/headlines").status_code == 200

async def test_get_headlines_picks_up_new_snapshot(client, mock_redis, monkeypatch):
    """A newly published snapshot is served once the local copy expires."""
    monkeypatch.setattr(settings, "LOCAL_CACHE_SECONDS", 0)
    await publish_snapshot(mock_redis, title="Old")
    assert client.get("/api/headlines").json()["data"][0]["title"] == "Old"

    await publish_snapshot(mock_redis, title="New")
    assert client.get("/api/headlines").json()["data"][0]["title"] == "New"

async def test_get_headlines_serves_last_snapshot_when_redis_down(client, mock_redis, monkeypatch):
    """If Redis fails, the last in-memory snapshot keeps being served."""
    monkeypatch.setattr(settings, "LOCAL_CACHE_SECONDS", 0)
    await publish_snapshot(mock_redis, title="Last good")
    client.get("/api/headlines")

    mock_redis.get = AsyncMock(side_effect=aioredis.ConnectionError("Redis is down"))
    response = client.get("/api/headlines")

    assert response.status_code == 200
    assert response.json()["data"][0]["title"] == "Last good"

# --- Test Rate Limiting ---

async def test_rate_limit_enforced_per_client_ip(client, mock_redis, monkeypatch):
    """Requests beyond API_RATE_LIMIT from one IP get 429; other IPs are unaffected."""
    limiter.reset()
    await publish_snapshot(mock_redis)
    limit = int(settings.API_RATE_LIMIT.split("/")[0])
    statuses = [client.get("/api/headlines", headers={"X-Real-IP": "198.51.100.1"}).status_code for _ in range(limit + 1)]
    assert statuses[:limit] == [200] * limit
    assert statuses[-1] == 429
    assert client.get("/api/headlines", headers={"X-Real-IP": "198.51.100.2"}).status_code == 200
    limiter.reset()


def make_request(headers, client_host="172.18.0.5"):
    scope = {
        "type": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": (client_host, 1234),
    }
    return Request(scope)

def test_real_ip_uses_header_set_by_caddy():
    assert get_real_ip(make_request({"X-Real-IP": "203.0.113.7"})) == "203.0.113.7"

def test_real_ip_ignores_cloudflare_header():
    """CF-Connecting-IP is resolved by Caddy, never trusted directly by the app."""
    assert get_real_ip(make_request({"CF-Connecting-IP": "1.2.3.4"})) == "172.18.0.5"

# --- Test Admin Endpoints ---

async def test_clear_cache_admin_success(client, mock_redis):
    """Tests that the admin endpoint requests a refresh without dropping the current snapshot."""
    await publish_snapshot(mock_redis)

    response = client.post("/admin/cache/clear", headers={"X-API-Key": "test-key"})

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "message": "Refresh requested"}
    assert await mock_redis.llen(settings.REFRESH_REQUEST_KEY) == 1
    assert await mock_redis.get(snapshot_key(ALL_VIEW)) is not None

def test_clear_cache_admin_no_key(client):
    """Tests that the admin endpoint fails without an API key."""
    response = client.post("/admin/cache/clear")
    assert response.status_code == 401 # auto_error=True in APIKeyHeader

def test_clear_cache_admin_wrong_key(client):
    """Tests that the admin endpoint fails with an incorrect API key."""
    response = client.post("/admin/cache/clear", headers={"X-API-Key": "wrong-key"})
    assert response.status_code == 403

# --- Test Exception Handling ---

def test_404_error_as_html(client):
    """Tests that a 404 error returns an HTML page for browser clients."""
    response = client.get("/non-existent-page", headers={"Accept": "text/html"})
    assert response.status_code == 404
    assert "text/html" in response.headers['content-type']
    assert "Sorry, the page you are looking for could not be found." in response.text
    assert_site_styled_error_page(response)

def assert_site_styled_error_page(response):
    """The site's own stylesheet and nothing else: no fonts or anything from other sites, under a policy."""
    assert '<link rel="stylesheet" href="/static/retro.css">' in response.text
    assert "https://" not in response.text and "<style" not in response.text and "<script" not in response.text
    assert response.headers["content-security-policy"].startswith("default-src 'none'; style-src 'self';")
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]

def test_404_error_as_json(client):
    """Tests that a 404 error returns a JSON response for API clients."""
    response = client.get("/non-existent-page", headers={"Accept": "application/json"})
    assert response.status_code == 404
    assert "application/json" in response.headers['content-type']
    assert response.json()['detail'] == "Not Found"

def test_500_error_in_endpoint_as_html(client):
    """Tests that an error within an endpoint returns a 500 HTML page."""
    with patch.object(app.state.headlines_cache, "get", side_effect=ValueError("Something broke badly")):
        response = client.get("/api/headlines", headers={"Accept": "text/html"})

    assert response.status_code == 500
    assert "text/html" in response.headers['content-type']
    # Check for text from the templates/500.html template
    assert "An unexpected server error occurred." in response.text
    assert_site_styled_error_page(response)

def test_500_error_in_template_rendering_as_html(client):
    """Tests that a template rendering failure returns the static fallback error page."""
    # Break template rendering both in the route and in the error handler
    crash = ValueError("Template engine crashed")
    with patch("routes.frontend.templates.TemplateResponse", side_effect=crash), \
         patch("middleware.exception.templates.TemplateResponse", side_effect=crash):
        response = client.get("/", headers={"Accept": "text/html"})

    assert response.status_code == 500
    assert "text/html" in response.headers['content-type']
    # Check for text from the static templates/error.html template
    assert "Something went wrong." in response.text
    assert_site_styled_error_page(response)

# --- Test Docs Endpoint ---

def test_docs_disabled_by_default(client):
    """Tests that the /docs endpoint is disabled by default."""
    response = client.get("/docs")
    assert response.status_code == 404

async def test_get_headlines_does_not_wait_for_slow_redis(client, mock_redis, monkeypatch):
    """While one request is stuck re-syncing from a hung Redis, others get the current copy at once."""
    import asyncio
    from services.headlines_cache import HeadlinesCache

    await publish_snapshot(mock_redis, title="Current")
    cache = HeadlinesCache(mock_redis, [ALL_VIEW])
    await cache.get(ALL_VIEW)
    monkeypatch.setattr(settings, "LOCAL_CACHE_SECONDS", 0)

    release = asyncio.Event()
    async def hung_get(key):
        await release.wait()
    mock_redis.get = hung_get

    stuck = asyncio.create_task(cache.get(ALL_VIEW))  # Takes the lock and hangs on Redis
    await asyncio.sleep(0)
    snapshot = await asyncio.wait_for(cache.get(ALL_VIEW), timeout=0.5)
    assert '"Current"' in snapshot.body

    release.set()
    await stuck

@pytest.mark.parametrize("country, region", [("IN", "asia"), ("GB", "europe"), ("BR", "americas"), ("T1", "all"), (None, "all")])
def test_page_starts_on_the_visitors_region(client, country, region):
    headers = {"Accept": "text/html", **({"CF-IPCountry": country} if country else {})}
    page = client.get("/", headers=headers).text
    assert json.loads(page.split('<script id="home-data" type="application/json">')[1].split("</script>")[0]) == region


@pytest.mark.parametrize("accept", [None, "*/*", "text/html", "text/html,application/json;q=0.9"])
def test_root_serves_the_page_unless_only_json_is_asked_for(client, accept):
    """Crawlers and tools often send */* or no Accept header: they get the page, not the status JSON."""
    response = client.get("/", headers={"Accept": accept} if accept else {})
    assert "text/html" in response.headers["content-type"]
    assert "Accept" in response.headers["vary"].split(", ")


def test_root_json_varies_on_accept(client):
    assert "Accept" in client.get("/", headers={"Accept": "application/json"}).headers["vary"].split(", ")


def test_page_describes_the_site_as_structured_data(client):
    page = client.get("/", headers={"Accept": "text/html"}).text
    data = json.loads(page.split('<script type="application/ld+json">')[1].split("</script>")[0])
    assert data["@type"] == "WebSite" and data["name"] == "news.txt"
    assert data["url"] == "https://testserver/"


def test_robots_txt_allows_crawling_and_points_to_the_sitemap(client):
    response = client.get("/robots.txt")
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == "User-agent: *\nAllow: /\n\nSitemap: https://testserver/sitemap.xml\n"


def test_sitemap_lists_the_page(client):
    response = client.get("/sitemap.xml")
    assert response.headers["content-type"].startswith("application/xml")
    assert "<loc>https://testserver/</loc>" in response.text
