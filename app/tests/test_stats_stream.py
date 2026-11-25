"""
Tests for the stream of fetched headlines the stats service reads: new links
only, each with its fingerprint from the tagger, never the feed's text, and never
a way to hold up or break a refresh.
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx
import yaml

from config import settings
from services import stats_stream
from services.stats_stream import STREAM, publish_new


def item(link="https://example.com/a", title="A headline", source="Publisher", published=1793500000.0):
    return {"title": title, "link": link, "source": source, "published": published, "feed_text": "x" * 700}


def fingerprints(request):
    """The tagger's /embed: a made-up fingerprint per headline (passed on as it is)."""
    return httpx.Response(200, json={"vectors": [f"v:{i['title']}" for i in json.loads(request.content)["items"]]})


@pytest.fixture(autouse=True)
def tagger():
    with respx.mock(assert_all_called=False) as router:
        yield router.post(f"{settings.TAGGER_URL}/embed").mock(side_effect=fingerprints)


async def publish(redis_client, results_by_source):
    async with httpx.AsyncClient() as http_client:
        await publish_new(http_client, redis_client, results_by_source)


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
    await publish(mock_redis, {"Feed": [item("https://a.example/1"), item("https://a.example/2")]})
    assert await mock_redis.xlen(STREAM) == 2


async def test_links_are_not_sent_twice(mock_redis):
    await publish(mock_redis, {"Feed": [item()]})
    await publish(mock_redis, {"Feed": [item()]})
    assert await mock_redis.xlen(STREAM) == 1


async def test_feed_text_is_not_sent(mock_redis):
    await publish(mock_redis, {"Feed": [item()]})
    assert set((await entries(mock_redis))[0]) == {"title", "link", "source", "published", "label", "vector"}


async def test_label_comes_from_the_feed_not_the_publisher(mock_redis):
    await publish(mock_redis, {"Sport Feed": [item(source="Some Paper")]})
    entry = (await entries(mock_redis))[0]
    assert entry["label"] == "sport" and entry["source"] == "Some Paper"


async def test_a_feed_without_a_label_sends_an_empty_one(mock_redis):
    await publish(mock_redis, {"Extra": [item()]})
    assert (await entries(mock_redis))[0]["label"] == ""


async def test_a_missing_date_sends_an_empty_one(mock_redis):
    await publish(mock_redis, {"Feed": [item(published=None)]})
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
    await publish(mock_redis, {"Feed": [item()]})
    assert calls[0]["maxlen"] == 3 and calls[0]["approximate"] is True


async def test_disabled_sends_nothing(mock_redis, monkeypatch):
    monkeypatch.setattr(settings, "STATS_STREAM_ENABLED", False)
    await publish(mock_redis, {"Feed": [item()]})
    assert await mock_redis.xlen(STREAM) == 0


class BrokenRedis:
    def pipeline(self, **kwargs):
        raise ConnectionError("no redis")


async def test_redis_failure_is_swallowed():
    await publish(BrokenRedis(), {"Feed": [item()]})


async def test_links_are_sent_again_after_a_failure(mock_redis):
    await publish(BrokenRedis(), {"Feed": [item()]})
    await publish(mock_redis, {"Feed": [item()]})
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
    await asyncio.wait_for(publish(HangingRedis(), {"Feed": [item()]}), 1)


async def test_sent_links_stay_bounded(mock_redis, monkeypatch):
    monkeypatch.setattr(stats_stream, "MAX_SENT_LINKS", 2)
    await publish(mock_redis, {"Feed": [item(f"https://a.example/{i}") for i in range(5)]})
    assert len(stats_stream._sent) == 2


async def test_long_titles_are_cut(mock_redis):
    await publish(mock_redis, {"Feed": [item(title="x" * 5000)]})
    assert len((await entries(mock_redis))[0]["title"]) == stats_stream.MAX_TITLE_CHARS


async def test_long_publisher_names_are_cut(mock_redis):
    await publish(mock_redis, {"Feed": [item(source="x" * 5000)]})
    assert len((await entries(mock_redis))[0]["source"]) == stats_stream.MAX_SOURCE_CHARS


async def test_oversized_links_are_not_sent(mock_redis):
    await publish(mock_redis, {"Feed": [item(link="https://a.example/" + "x" * 5000)]})
    assert await mock_redis.xlen(STREAM) == 0


def test_the_off_switch_reaches_the_refresher():
    compose = yaml.safe_load((Path(__file__).parents[2] / "docker-compose.yml").read_text())
    assert "STATS_STREAM_ENABLED" in compose["services"]["refresher"]["environment"]


async def test_entries_match_the_sample_the_worker_is_tested_with(mock_redis):
    sample = json.loads((Path(__file__).parents[2] / "stats/data/stream_entry.json").read_text())
    await publish(mock_redis, {"Feed": [item()]})
    assert STREAM == sample["stream"] and set((await entries(mock_redis))[0]) == set(sample["fields"])


async def test_each_entry_carries_its_fingerprint(mock_redis, tagger):
    await publish(mock_redis, {"Feed": [item(title="Kenya votes - Paper", source="Paper")]})
    assert json.loads(tagger.calls.last.request.content) == {"items": [{"title": "Kenya votes - Paper", "source": "Paper"}]}
    assert (await entries(mock_redis))[0]["vector"] == "v:Kenya votes - Paper"


async def test_links_wait_while_the_tagger_is_down(mock_redis, tagger):
    tagger.side_effect = [httpx.ConnectError("starting"), fingerprints]
    await publish(mock_redis, {"Feed": [item()]})
    assert await mock_redis.xlen(STREAM) == 0
    await publish(mock_redis, {"Feed": [item()]})
    assert await mock_redis.xlen(STREAM) == 1


async def test_many_new_links_go_in_chunks(mock_redis, tagger, monkeypatch):
    monkeypatch.setattr(stats_stream, "EMBED_CHUNK", 2)
    await publish(mock_redis, {"Feed": [item(f"https://a.example/{i}") for i in range(5)]})
    assert tagger.call_count == 3 and await mock_redis.xlen(STREAM) == 5


async def test_chunks_written_before_the_time_runs_out_stay_sent(mock_redis, tagger, monkeypatch):
    monkeypatch.setattr(stats_stream, "EMBED_CHUNK", 1)
    monkeypatch.setattr(settings, "STATS_STREAM_TIMEOUT_SECONDS", 0.5)
    asked = []
    async def slow_second(request):
        asked.append(request)
        if len(asked) > 1:
            await asyncio.sleep(5)
        return fingerprints(request)
    tagger.side_effect = slow_second
    await publish(mock_redis, {"Feed": [item("https://a.example/1"), item("https://a.example/2")]})
    assert [e["link"] for e in await entries(mock_redis)] == ["https://a.example/1"]
    assert list(stats_stream._sent) == ["https://a.example/1"]  # The other goes next time


def test_a_chunk_of_new_headlines_is_fingerprinted_well_within_the_time():
    # The tagger embeds 64-84 new headlines a second (one story model compile, at summaries' length; measured
    # Nov 25, 2026). A chunk that can't finish in time would be asked again every refresh, and nothing would
    # reach the stream.
    assert stats_stream.EMBED_CHUNK / 64 <= settings.STATS_STREAM_TIMEOUT_SECONDS / 2
