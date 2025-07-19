import pytest
import pytest_asyncio
import json
import time
import os
from unittest.mock import AsyncMock, patch
import asyncio
from importlib import reload

from fastapi.testclient import TestClient
from fakeredis.aioredis import FakeRedis
import respx
import httpx

# We need to import the app object from main
from main import app
from config import settings

# --- Mock Data ---

# Using a fixed time for reproducible tests
FIXED_TIME_UNIX = 1672531200 # 2023-01-01 00:00:00

# --- Mock Data for Source 1: Custom Search (70% weight -> 21 articles) ---
# We provide more than 21 to ensure truncation logic is tested.
CUSTOM_SEARCH_URL = settings.NEWS_SOURCES["Google News (Custom Search)"]
CUSTOM_SEARCH_LINKS = [f"https://news.google.com/rss/articles/custom-link-{i}" for i in range(1, 26)]
CUSTOM_SEARCH_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
    {''.join([f'''<item>
        <title>Custom Article {i}</title>
        <link>{CUSTOM_SEARCH_LINKS[i-1]}</link>
        <pubDate>{time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime(FIXED_TIME_UNIX - i * 10))}</pubDate>
        <source url="https://custom.source">Custom Source</source>
    </item>''' for i in range(1, 26)])}
</channel></rss>
"""

# --- Mock Data for Source 2: Science (30% weight -> 9 articles) ---
# We provide more than 9 to ensure truncation logic is tested.
SCIENCE_URL = settings.NEWS_SOURCES["Google News (Science)"]
SCIENCE_LINKS = [f"https://news.google.com/rss/articles/science-link-{i}" for i in range(1, 11)]
SCIENCE_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
    {''.join([f'''<item>
        <title>Science Article {i}</title>
        <link>{SCIENCE_LINKS[i-1]}</link>
        <pubDate>{time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime(FIXED_TIME_UNIX - i * 10 - 5))}</pubDate>
        <source url="https://science.source">Science Source</source>
    </item>''' for i in range(1, 11)])}
</channel></rss>
"""

# Use pytest-asyncio for all async tests in this module
pytestmark = pytest.mark.asyncio


# --- Fixtures ---

@pytest_asyncio.fixture(scope="function")
async def mock_redis():
    """Fixture to provide a mock redis client for each test function."""
    redis_client = FakeRedis(decode_responses=True)    
    yield redis_client
    # In newer versions of fakeredis, close() might not be async
    if hasattr(redis_client, 'close') and asyncio.iscoroutinefunction(redis_client.close):
        await redis_client.close()

@pytest.fixture(scope="function")
def client(mock_redis, monkeypatch):
    """Fixture to provide a TestClient instance with mocked redis and settings."""
    # Set a dummy admin key for testing
    monkeypatch.setattr(settings, 'ADMIN_API_KEY', 'test-key')
    monkeypatch.setattr(settings, 'DEBUG', True) # Ensure debug features are on for tests

    app.state.redis = mock_redis
    with TestClient(app) as test_client:
        yield test_client


# --- Test Endpoints ---

def test_health_check_success(client):
    """Tests that the /health endpoint returns 200 when Redis is available."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["redis"] is True
    assert data["version"] == settings.APP_VERSION

def test_health_check_redis_failure(client, mock_redis):
    """Tests that the /health endpoint returns 503 when Redis is down."""
    mock_redis.ping = AsyncMock(side_effect=ConnectionError("Redis is down"))
    response = client.get("/health")
    assert response.status_code == 503
    data = response.json()
    assert data["status"] == "unhealthy"
    assert data["redis"] is False
    assert data["version"] == settings.APP_VERSION

def test_root_endpoint_html(client):
    """Tests that the root endpoint serves HTML to browser clients."""
    response = client.get("/", headers={"Accept": "text/html"})
    assert response.status_code == 200
    assert "text/html" in response.headers['content-type']
    assert "<h1>Latest News</h1>" in response.text

def test_root_endpoint_json(client):
    """Tests that the root endpoint serves JSON to API clients."""
    response = client.get("/", headers={"Accept": "application/json"})
    assert response.status_code == 200
    assert "application/json" in response.headers['content-type']
    data = response.json()
    assert data["message"] == "News Aggregator API is running."
    assert data["version"] == settings.APP_VERSION

async def test_get_headlines_cache_hit(client, mock_redis):
    """Tests the /api/headlines endpoint when data is found in the cache."""
    now = time.time()
    expires_at = now + 60
    cached_headlines = [{
        "title": "Cached News",
        "link": "http://cache.com",
        "source": "BBC",
        "published": FIXED_TIME_UNIX,
        "last_updated": now
    }]
    cache_data = {
        "data": cached_headlines,
        "source": "cache_revalidated", # This will be the source from the cache
        "expires_at": expires_at,
        "ttl": 60 # Add the original TTL to the mock cached data
    }
    await mock_redis.setex(settings.CACHE_KEY, 60, json.dumps(cache_data))
    
    response = client.get("/api/headlines")

    assert response.status_code == 200
    assert "cache-control" in response.headers
    assert "public, max-age=" in response.headers["cache-control"]

    data = response.json()
    assert data["source"] == "cache_revalidated"
    assert len(data["data"]) == 1
    assert data["data"][0]["title"] == "Cached News"
    assert "ttl" in data
    assert data["ttl"] <= 60

@respx.mock
async def test_get_headlines_cache_miss_with_weighting(client, mock_redis):
    """
    Tests a cache miss, verifying that the app fetches from all sources, applies
    the correct weighting, shuffles the result, resolves the links, and caches
    the final list.
    """
    # 1. Mock the initial fetch for both RSS feeds
    respx.get(CUSTOM_SEARCH_URL).mock(return_value=httpx.Response(200, content=CUSTOM_SEARCH_XML))
    respx.get(SCIENCE_URL).mock(return_value=httpx.Response(200, content=SCIENCE_XML))

    # 2. Mock the redirect resolution for the top N links from each source
    # The app will take 21 from custom and 9 from science.
    redirect_headers = {
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36',
    }
    for i in range(21):
        respx.get(CUSTOM_SEARCH_LINKS[i], headers=redirect_headers).mock(return_value=httpx.Response(200, url=f"https://resolved.custom/article-{i+1}"))
    for i in range(9):
        respx.get(SCIENCE_LINKS[i], headers=redirect_headers).mock(return_value=httpx.Response(200, url=f"https://resolved.science/article-{i+1}"))

    # 3. Make the request to the API
    response = client.get("/api/headlines")

    # 4. Assertions
    assert response.status_code == 200
    data = response.json()
    headlines = data["data"]

    assert data["source"] == "live"
    assert len(headlines) == settings.MAX_HEADLINES

    # Verify weighting by counting articles from each source in the shuffled list
    custom_articles_count = sum(1 for h in headlines if 'Custom Article' in h['title'])
    science_articles_count = sum(1 for h in headlines if 'Science Article' in h['title'])

    # 70% of 30 is 21, 30% is 9
    assert custom_articles_count == 21
    assert science_articles_count == 9

    # Verify that the links were resolved
    resolved_links = {h['link'] for h in headlines}
    assert "https://resolved.custom/article-1" in resolved_links
    assert "https://resolved.science/article-1" in resolved_links

    # Verify that the data is now in the cache
    cached = await mock_redis.get(settings.CACHE_KEY)
    assert cached is not None
    cached_data = json.loads(cached)
    assert len(cached_data['data']) == settings.MAX_HEADLINES

@respx.mock
async def test_get_headlines_redis_error_fallback(client, mock_redis):
    """Tests that a live fetch is performed when Redis GET fails."""
    # Mock Redis to raise an error on GET
    mock_redis.get = AsyncMock(side_effect=ConnectionError("Redis is down"))

    # Mock the external fetches so the fallback can succeed
    respx.get(CUSTOM_SEARCH_URL).mock(return_value=httpx.Response(200, content=CUSTOM_SEARCH_XML))
    respx.get(SCIENCE_URL).mock(return_value=httpx.Response(200, content=SCIENCE_XML))
    for link in CUSTOM_SEARCH_LINKS + SCIENCE_LINKS:
        respx.get(link).mock(return_value=httpx.Response(200, url=f"https://resolved.fallback/{link.split('/')[-1]}"))

    response = client.get("/api/headlines")

    assert response.status_code == 200
    data = response.json()
    assert data["source"] == "fallback_redis_error"
    assert len(data["data"]) == settings.MAX_HEADLINES
    assert data["expires_at"] is None

@respx.mock
async def test_get_headlines_lock_wait(client, mock_redis):
    """Tests the scenario where a process waits for another to populate the cache."""
    # 1. Ensure cache is empty
    await mock_redis.delete(settings.CACHE_KEY)

    # 2. Mock the lock to be already held
    await mock_redis.set(settings.LOCK_KEY, "some-other-process-id", ex=15)

    # 3. Define a background task that will populate the cache after a short delay
    async def populate_cache_later():
        await asyncio.sleep(1) # Wait 1 second
        cached_data = {"data": [{"title": "Populated by leader", "link": "x", "source": "y", "published": 1, "last_updated": 1}], "source": "live", "expires_at": time.time() + 60}
        await mock_redis.setex(settings.CACHE_KEY, 60, json.dumps(cached_data))

    # 4. Start the background task and make the API request concurrently
    task = asyncio.create_task(populate_cache_later())
    response = client.get("/api/headlines")
    await task # Ensure the background task completes

    # 5. Assertions
    assert response.status_code == 200
    data = response.json()
    assert data["source"] == "live" # It gets the data populated by the leader
    assert data["data"][0]["title"] == "Populated by leader"

# --- Test Admin Endpoints ---

async def test_clear_cache_admin_success(client, mock_redis):
    """Tests that the admin cache clear endpoint works with a valid key."""
    await mock_redis.set(settings.CACHE_KEY, "some_value")
    
    response = client.post("/admin/cache/clear", headers={"X-API-Key": "test-key"})
    
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "message": "Cache cleared"}
    
    # Verify the key is gone
    val = await mock_redis.get(settings.CACHE_KEY)
    assert val is None

def test_clear_cache_admin_no_key(client):
    """Tests that the admin endpoint fails without an API key."""
    response = client.post("/admin/cache/clear")
    assert response.status_code == 403 # auto_error=True in APIKeyHeader

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

def test_404_error_as_json(client):
    """Tests that a 404 error returns a JSON response for API clients."""
    response = client.get("/non-existent-page", headers={"Accept": "application/json"})
    assert response.status_code == 404
    assert "application/json" in response.headers['content-type']
    assert response.json()['error'] == "Not Found"

def test_500_error_in_endpoint_as_html(client):
    """Tests that an error within an endpoint returns a 500 HTML page."""
    # We patch the `get_headlines` endpoint to simulate an unexpected internal error.
    with patch("main.get_headlines", side_effect=ValueError("Something broke badly")):
        response = client.get("/api/headlines", headers={"Accept": "text/html"})

    assert response.status_code == 500
    assert "text/html" in response.headers['content-type']
    # Check for text from the /home/pi/news-aggregator/app/templates/500.html template
    assert "An unexpected server error occurred." in response.text

def test_500_error_in_template_rendering_as_html(client):
    """Tests that a template rendering failure returns the static fallback error page."""
    # We patch the template rendering to simulate an unexpected internal error.
    with patch("main.templates.TemplateResponse", side_effect=ValueError("Template engine crashed")):
        response = client.get("/", headers={"Accept": "text/html"})

    assert response.status_code == 500
    assert "text/html" in response.headers['content-type']
    # Check for text from the static /home/pi/news-aggregator/app/templates/error.html template
    assert "<h1>Something went wrong.</h1>" in response.text

# --- Test Docs Endpoint ---

def test_docs_disabled_by_default(client):
    """Tests that the /docs endpoint is disabled by default."""
    response = client.get("/docs")
    assert response.status_code == 404

@patch.dict(os.environ, {"ENABLE_API_DOCS": "true"})
def test_docs_enabled_via_env(mock_redis):
    """Tests that the /docs endpoint can be enabled via an environment variable."""
    # We need to reload the modules to re-evaluate the app definition with the new env var
    reload(settings)
    # We must import the app *after* the settings have been reloaded
    from main import app as reloaded_app
    # A new client instance is needed to use the reloaded app
    with TestClient(reloaded_app) as new_client:
        new_client.app.state.redis = mock_redis # re-attach mock redis
        response = new_client.get("/docs")
        assert response.status_code == 200
        assert "text/html" in response.headers['content-type']
