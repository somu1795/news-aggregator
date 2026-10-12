"""
Shared test fixtures for the News Aggregator test suite.
"""

import os

# Must be set before the app (and its TrustedHostMiddleware) is imported. Forced, not
# defaulted, so values from the caller's shell (e.g. a sourced .env) can't leak in.
os.environ["ALLOWED_HOSTS"] = "testserver"
# Rate limiter storage; the app's own Redis client is replaced by FakeRedis below
os.environ["REDIS_URL"] = "memory://"

import pytest
import pytest_asyncio

from fastapi.testclient import TestClient
from fakeredis.aioredis import FakeRedis

import main
from main import app
from config import settings


# Use pytest-asyncio for all async tests in this module
pytestmark = pytest.mark.asyncio


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
