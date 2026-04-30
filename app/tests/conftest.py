"""
Shared test fixtures for the News Aggregator test suite.
"""

import asyncio

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
from fakeredis.aioredis import FakeRedis

from main import app
from config import settings
from services.cache_service import register_lock_script


# Use pytest-asyncio for all async tests in this module
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(scope="function")
async def mock_redis():
    """Fixture to provide a mock redis client for each test function."""
    redis_client = FakeRedis(decode_responses=True)
    yield redis_client
    if hasattr(redis_client, "close") and asyncio.iscoroutinefunction(redis_client.close):
        await redis_client.close()


@pytest.fixture(scope="function")
def client(mock_redis, monkeypatch):
    """Fixture to provide a TestClient instance with mocked redis and settings."""
    monkeypatch.setattr(settings, "ADMIN_API_KEY", "test-key")
    monkeypatch.setattr(settings, "DEBUG", True)

    app.state.redis = mock_redis
    app.state.release_lock_script = register_lock_script(mock_redis)
    with TestClient(app) as test_client:
        yield test_client
