"""
Tests for the stream of fetched headlines the stats service reads: new links
only, never the feed's text, and never a way to hold up or break a refresh.
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from config import settings
from services import stats_stream
from services.stats_stream import STREAM, publish_new

pytestmark = pytest.mark.asyncio


def item(link="https://example.com/a", title="A headline", source="Publisher", published=1793500000.0):
    return {"title": title, "link": link, "source": source, "published": published, "feed_text": "x" * 700}


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    stats_stream._sent.clear()
    catalog = SimpleNamespace(
        regions=[SimpleNamespace(sources=[SimpleNamespace(name="Feed", label="other")],
                                 topic_sources=[SimpleNamespace(name="Sport Feed", label="sport")])],
        extras=[SimpleNamespace(name="Extra", label=None)],
        topic_extras=[],
    )
    monkeypatch.setattr(stats_stream, "get_catalog", lambda: catalog)


async def entries(redis_client):
    return [fields for _, fields in await redis_client.xrange(STREAM)]


async def test_new_links_reach_the_stream(mock_redis):
    await publish_new(mock_redis, {"Feed": [item("https://a.example/1"), item("https://a.example/2")]})
    assert await mock_redis.xlen(STREAM) == 2


async def test_links_are_not_sent_twice(mock_redis):
    await publish_new(mock_redis, {"Feed": [item()]})
    await publish_new(mock_redis, {"Feed": [item()]})
    assert await mock_redis.xlen(STREAM) == 1


async def test_feed_text_is_not_sent(mock_redis):
    await publish_new(mock_redis, {"Feed": [item()]})
    assert set((await entries(mock_redis))[0]) == {"title", "link", "source", "published", "label"}


async def test_label_comes_from_the_feed_not_the_publisher(mock_redis):
    await publish_new(mock_redis, {"Sport Feed": [item(source="Some Paper")]})
    entry = (await entries(mock_redis))[0]
    assert entry["label"] == "sport" and entry["source"] == "Some Paper"


async def test_a_feed_without_a_label_sends_an_empty_one(mock_redis):
    await publish_new(mock_redis, {"Extra": [item()]})
    assert (await entries(mock_redis))[0]["label"] == ""


async def test_a_missing_date_sends_an_empty_one(mock_redis):
    await publish_new(mock_redis, {"Feed": [item(published=None)]})
    assert (await entries(mock_redis))[0]["published"] == ""


async def test_stream_is_capped(mock_redis, monkeypatch):
    monkeypatch.setattr(settings, "STATS_STREAM_MAXLEN", 3)
    calls = []
    real = mock_redis.pipeline

    class Recording:
        def __init__(self):
            self.pipe = real()

        def xadd(self, *args, **kwargs):
            calls.append(kwargs)
            return self.pipe.xadd(*args, **kwargs)

        async def execute(self):
            return await self.pipe.execute()

    monkeypatch.setattr(mock_redis, "pipeline", lambda **kwargs: Recording())
    await publish_new(mock_redis, {"Feed": [item()]})
    assert calls[0]["maxlen"] == 3 and calls[0]["approximate"] is True


async def test_disabled_sends_nothing(mock_redis, monkeypatch):
    monkeypatch.setattr(settings, "STATS_STREAM_ENABLED", False)
    await publish_new(mock_redis, {"Feed": [item()]})
    assert await mock_redis.xlen(STREAM) == 0


class BrokenRedis:
    def pipeline(self, **kwargs):
        raise ConnectionError("no redis")


async def test_redis_failure_is_swallowed():
    await publish_new(BrokenRedis(), {"Feed": [item()]})


async def test_links_are_sent_again_after_a_failure(mock_redis):
    await publish_new(BrokenRedis(), {"Feed": [item()]})
    await publish_new(mock_redis, {"Feed": [item()]})
    assert await mock_redis.xlen(STREAM) == 1


class HangingPipe:
    def xadd(self, *args, **kwargs):
        pass

    async def execute(self):
        await asyncio.Event().wait()


class HangingRedis:
    def pipeline(self, **kwargs):
        return HangingPipe()


async def test_slow_redis_times_out(monkeypatch):
    monkeypatch.setattr(settings, "STATS_STREAM_TIMEOUT_SECONDS", 0.01)
    await asyncio.wait_for(publish_new(HangingRedis(), {"Feed": [item()]}), 1)


async def test_sent_links_stay_bounded(mock_redis, monkeypatch):
    monkeypatch.setattr(stats_stream, "MAX_SENT_LINKS", 2)
    await publish_new(mock_redis, {"Feed": [item(f"https://a.example/{i}") for i in range(5)]})
    assert len(stats_stream._sent) == 2


async def test_long_titles_are_cut(mock_redis):
    await publish_new(mock_redis, {"Feed": [item(title="x" * 5000)]})
    assert len((await entries(mock_redis))[0]["title"]) == stats_stream.MAX_TITLE_CHARS


async def test_long_publisher_names_are_cut(mock_redis):
    await publish_new(mock_redis, {"Feed": [item(source="x" * 5000)]})
    assert len((await entries(mock_redis))[0]["source"]) == stats_stream.MAX_SOURCE_CHARS


async def test_oversized_links_are_not_sent(mock_redis):
    await publish_new(mock_redis, {"Feed": [item(link="https://a.example/" + "x" * 5000)]})
    assert await mock_redis.xlen(STREAM) == 0


def test_the_off_switch_reaches_the_refresher():
    compose = yaml.safe_load((Path(__file__).parents[2] / "docker-compose.yml").read_text())
    assert "STATS_STREAM_ENABLED" in compose["services"]["refresher"]["environment"]


def test_sent_links_cost_little_memory():
    assert stats_stream.MAX_SENT_LINKS == 50_000


async def test_entries_match_the_sample_the_worker_is_tested_with(mock_redis):
    sample = json.loads((Path(__file__).parents[2] / "stats/data/stream_entry.json").read_text())
    await publish_new(mock_redis, {"Feed": [item()]})
    assert STREAM == sample["stream"] and set((await entries(mock_redis))[0]) == set(sample["fields"])
