"""
The app's side of the tagger's HTTP interface: what it sends and reads matches tagger/data/api_sample.json,
which the tagger's own tests answer to (tagger/test_tagger.py). Each side is otherwise tested against a fake of the other.
"""

import json
from pathlib import Path

import httpx
import respx

import summarizer
from catalog import Catalog
from config import settings
from refresher import sync_stories
from services import archive
from services.story_groups import group_stories
from services.views.topic_views import tag

API = json.loads((Path(__file__).parents[2] / "tagger/data/api_sample.json").read_text())
CATALOG = Catalog(regions=[{"id": "asia", "label": "Asia", "sources": [
    {"name": "Feed", "url": "https://feed.example/rss", "weight": 1.0, "label": "other"}]}], extras=[])


def route(name, method="post"):
    """Mocks a tagger route with the sample's answer; returns the route."""
    return getattr(respx, method)(f"{settings.TAGGER_URL}/{name}").mock(return_value=httpx.Response(200, json=API[name]["response"]))


def sent(route):
    return json.loads(route.calls.last.request.content)


def headlines(name):
    """The sample request's items as fetched headlines."""
    return [{"link": f"https://example.com/{n}", **item, "published": 1.0} for n, item in enumerate(API[name]["request"]["items"])]


@respx.mock
async def test_tagging_sends_and_reads_what_the_sample_says(mock_redis):
    tagger = route("tag")
    async with httpx.AsyncClient() as client:
        tags, misfiled = await tag(client, mock_redis, CATALOG, {"Feed": headlines("tag")})
    assert sent(tagger) == API["tag"]["request"]
    assert tags == {API["tag"]["request"]["items"][0]["title"]: set(API["tag"]["response"]["tags"][0])} and misfiled == set()


@respx.mock
async def test_grouping_sends_and_reads_what_the_sample_says():
    tagger = route("group")
    async with httpx.AsyncClient() as client:
        grouped = await group_stories(client, {"all": headlines("group")})
    assert sent(tagger) == API["group"]["request"]
    assert [item["story"] for item in grouped["all"]] == API["group"]["response"]["groups"]


@respx.mock
async def test_the_archive_follows_stories_as_the_sample_says(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ARCHIVE_PATH", str(tmp_path / "archive.db"))
    archive.archive({"all": headlines("stories")}, 1000.0)
    moves = respx.get(f"{settings.TAGGER_URL}/story-moves").mock(side_effect=[
        httpx.Response(200, json=API["story-moves"]["response"]), httpx.Response(200, json={"moves": []})])
    stories = route("stories")
    async with httpx.AsyncClient() as client:
        await sync_stories(client)
    assert [dict(call.request.url.params) for call in moves.calls] == [{"after": "0"}, {"after": str(API["story-moves"]["response"]["moves"][-1]["id"])}]
    assert sent(stories) == API["stories"]["request"]
    assert archive.without_story(10) == [] and archive.last_story_move() == API["story-moves"]["response"]["moves"][-1]["id"]


@respx.mock
async def test_story_pairs_are_judged_as_the_sample_says(monkeypatch, mock_redis):
    monkeypatch.setattr(settings, "SUMMARIZER_LLM_URL", "http://llm.example:11434")
    monkeypatch.setattr(settings, "SUMMARIZER_MODEL", API["story-verdicts"]["request"]["verdicts"][0]["model"])
    respx.get(f"{settings.TAGGER_URL}/story-candidates").mock(side_effect=[
        httpx.Response(200, json=API["story-candidates"]["response"]), httpx.Response(200, json={"pairs": []})])
    respx.post("http://llm.example:11434/api/chat").mock(
        return_value=httpx.Response(200, json={"message": {"role": "assistant", "content": "Yes"}}))
    verdicts = route("story-verdicts")
    async with httpx.AsyncClient() as client:
        assert await summarizer.judge_stories(client, mock_redis) == 1
    assert sent(verdicts) == API["story-verdicts"]["request"]


@respx.mock
async def test_summaries_are_sent_as_the_sample_says(mock_redis):
    tagger = route("summaries")
    [sample] = API["summaries"]["request"]["items"]
    async with httpx.AsyncClient() as client:
        await summarizer.send_summaries(client, [({"title": sample["title"], "source": sample["source"], "link": "https://example.com/0"},
                                                  sample["summary"])])
    assert sent(tagger) == API["summaries"]["request"]
