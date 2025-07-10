import pytest
import pytest_asyncio
import json
import time
import os
from unittest.mock import AsyncMock, patch
import asyncio

from fastapi.testclient import TestClient
from fakeredis.aioredis import FakeRedis
import respx
import httpx

# We need to import the app object from main
from main import app
import config

# --- Mock Data ---

# Using a fixed time for reproducible tests
FIXED_TIME_UNIX = 1672531200 # 2023-01-01 00:00:00

# --- Mock Data for Source 1: Custom Search (70% weight -> 14 articles) ---
# We provide more than 14 to ensure truncation logic is tested.
CUSTOM_SEARCH_URL = config.NEWS_SOURCES["Google News (Custom Search)"]
CUSTOM_SEARCH_LINKS = [f"https://news.google.com/rss/articles/custom-link-{i}" for i in range(1, 16)]
CUSTOM_SEARCH_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
    {''.join([f'''<item>
        <title>Custom Article {i}</title>
        <link>{CUSTOM_SEARCH_LINKS[i-1]}</link>
        <pubDate>{time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime(FIXED_TIME_UNIX - i * 10))}</pubDate>
        <source url="https://custom.source">Custom Source</source>
    </item>''' for i in range(1, 16)])}
</channel></rss>
"""

# --- Mock Data for Source 2: Science (30% weight -> 6 articles) ---
# We provide more than 6 to ensure truncation logic is tested.
SCIENCE_URL = config.NEWS_SOURCES["Google News (Science)"]
SCIENCE_LINKS = [f"https://news.google.com/rss/articles/science-link-{i}" for i in range(1, 8)]
SCIENCE_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
    {''.join([f'''<item>
        <title>Science Article {i}</title>
        <link>{SCIENCE_LINKS[i-1]}</link>
        <pubDate>{time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime(FIXED_TIME_UNIX - i * 10 - 5))}</pubDate>
        <source url="https://science.source">Science Source</source>
    </item>''' for i in range(1, 8)])}
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
    """Fixture to provide a TestClient instance with mocked redis."""
    # Mock the httpx client on app.state
    app.state.http_client = httpx.AsyncClient()
    # Set a dummy admin key for testing
    monkeypatch.setenv("ADMIN_API_KEY", "test-key")
    app.state.redis = mock_redis
    yield TestClient(app)


# --- Test Endpoints ---

def test_health_check_success(client):
    """Tests that the /health endpoint returns 200 when Redis is available."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["redis"] is True

def test_health_check_redis_failure(client, mock_redis):
    """Tests that the /health endpoint returns 503 when Redis is down."""
    mock_redis.ping = AsyncMock(side_effect=ConnectionError("Redis is down"))
    response = client.get("/health")
    assert response.status_code == 503
    data = response.json()
    assert data["status"] == "unhealthy"
    assert data["redis"] is False

async def test_get_headlines_cache_hit(client, mock_redis):
    """Tests the /api/headlines endpoint when data is found in the cache."""
    expires_at = time.time() + 60
    cached_headlines = [{"title": "Cached News", "link": "http://cache.com", "source": "BBC", "published": FIXED_TIME_UNIX}]
    cache_data = {"data": cached_headlines, "source": "cache", "cached_until": expires_at}
    await mock_redis.set(config.CACHE_KEY, json.dumps(cache_data))
    
    response = client.get("/api/headlines")

    assert response.status_code == 200
    data = response.json()
    assert data["source"] == "cache"
    assert len(data["data"]) == 1
    assert data["data"][0]["title"] == "Cached News"

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
    # The app will take 14 from custom and 6 from science.
    redirect_headers = {
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36',
    }
    for i in range(14):
        respx.get(CUSTOM_SEARCH_LINKS[i], headers=redirect_headers).mock(return_value=httpx.Response(200, url=f"https://resolved.custom/article-{i+1}"))
    for i in range(6):
        respx.get(SCIENCE_LINKS[i], headers=redirect_headers).mock(return_value=httpx.Response(200, url=f"https://resolved.science/article-{i+1}"))

    # 3. Make the request to the API
    response = client.get("/api/headlines")

    # 4. Assertions
    assert response.status_code == 200
    data = response.json()
    headlines = data["data"]

    assert data["source"] == "live"
    assert len(headlines) == config.MAX_HEADLINES

    # Verify weighting by counting articles from each source in the shuffled list
    custom_articles_count = sum(1 for h in headlines if 'Custom Article' in h['title'])
    science_articles_count = sum(1 for h in headlines if 'Science Article' in h['title'])

    # 70% of 20 is 14, 30% is 6
    assert custom_articles_count == 14
    assert science_articles_count == 6

    # Verify that the links were resolved
    resolved_links = {h['link'] for h in headlines}
    assert "https://resolved.custom/article-1" in resolved_links
    assert "https://resolved.science/article-1" in resolved_links

    # Verify that the data is now in the cache
    cached = await mock_redis.get(config.CACHE_KEY)
    assert cached is not None
    cached_data = json.loads(cached)
    assert len(cached_data['data']) == config.MAX_HEADLINES

# --- Test Admin Endpoints ---

async def test_clear_cache_admin_success(client, mock_redis):
    """Tests that the admin cache clear endpoint works with a valid key."""
    await mock_redis.set(config.CACHE_KEY, "some_value")
    
    response = client.post("/admin/cache/clear", headers={"X-API-Key": "test-key"})
    
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "message": "Cache cleared"}
    
    # Verify the key is gone
    val = await mock_redis.get(config.CACHE_KEY)
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

def test_500_error_as_html(client):
    """Tests that a generic 500 error returns an HTML page for browser clients."""
    # We patch the `serve_frontend` endpoint's template rendering to simulate
    # an unexpected internal error that is not an HTTPException.
    with patch("main.templates.TemplateResponse", side_effect=ValueError("Template engine crashed")):
        response = client.get("/", headers={"Accept": "text/html"})

    assert response.status_code == 500
    assert "text/html" in response.headers['content-type']
    # Check for text from the /home/pi/news-aggregator/app/templates/500.html template
    assert "An unexpected server error occurred." in response.text

# --- Test Docs Endpoint ---

def test_docs_disabled_by_default(client):
    """Tests that the /docs endpoint is disabled by default."""
    response = client.get("/docs")
    assert response.status_code == 404

@patch.dict(os.environ, {"ENABLE_API_DOCS": "true"})
def test_docs_enabled_via_env(mock_redis):
    """Tests that the /docs endpoint can be enabled via an environment variable."""
    # We need a new client instance to re-evaluate the app definition
    with TestClient(app) as new_client:
        response = new_client.get("/docs")
        assert response.status_code == 200
        assert "text/html" in response.headers['content-type']
