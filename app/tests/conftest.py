"""
Shared test fixtures for the News Aggregator test suite.
"""

import os

# Must be set before the app (and its TrustedHostMiddleware) is imported. Forced, not
# defaulted, so values from the caller's shell (e.g. a sourced .env) can't leak in.
os.environ["ALLOWED_HOSTS"] = "testserver"
# Rate limiter storage; the app's own Redis client is replaced by FakeRedis below
os.environ["REDIS_URL"] = "memory://"
# The tests are about the site this repo ships (its regions, topics and name), whatever CONFIG_DIR the caller runs
os.environ["CONFIG_DIR"] = os.path.join(os.path.dirname(__file__), "..", "..", "config")

import pytest
import pytest_asyncio

from fastapi.testclient import TestClient
from fakeredis.aioredis import FakeRedis

import main
from main import app
from config import settings
from deps import limiter
from services.feeds.poller import FeedPoller, HostLimits
from services.links import google_news


@pytest.fixture(autouse=True)
def no_request_jitter(monkeypatch):
    monkeypatch.setattr(settings, "FEED_JITTER_SECONDS", 0.0)
    monkeypatch.setattr(settings, "FEED_HOST_GAP_SECONDS", 0.0)
    monkeypatch.setattr(settings, "LINK_RESOLVE_GAP_SECONDS", 0.0)
    monkeypatch.setattr(google_news, "_GOOGLE", HostLimits())  # Google's pauses don't carry from test to test


@pytest.fixture(autouse=True)
def fresh_rate_limits():
    """Each test starts with no requests counted: the tests all come from one address, within a minute."""
    limiter.reset()


@pytest.fixture
def poller():
    """A refresher's feed memory, empty: no remembered feeds or paused hosts."""
    return FeedPoller()


@pytest_asyncio.fixture(scope="function")
async def mock_redis():
    """Fixture to provide a mock redis client for each test function."""
    redis_client = FakeRedis(decode_responses=True)
    yield redis_client
    await redis_client.aclose()


@pytest.fixture(scope="function")
def client(mock_redis, monkeypatch):
    """Fixture to provide a TestClient instance with mocked redis and settings."""
    monkeypatch.setattr(settings, "ADMIN_API_KEY", "test-key")
    monkeypatch.setattr(settings, "DEBUG", True)
    monkeypatch.setattr(main, "create_redis_client", lambda **kwargs: mock_redis)

    with TestClient(app) as test_client:
        yield test_client
