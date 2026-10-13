"""Tests for folding other outlets' headlines about the same story under its first one."""

import httpx
import pytest
import respx

from config import settings
from services.story_groups import group_stories

pytestmark = pytest.mark.asyncio


def item(title, source="Pub"):
    return {"title": title, "link": f"https://{source.lower()}.example/{title.replace(' ', '-')}", "source": source}


def tagger_groups(groups_by_title):
    """Mocks the tagger's /group with fixed groups (by title); returns the route."""
    def reply(request):
        import json
        titles = [i["title"] for i in json.loads(request.content)["items"]]
        return httpx.Response(200, json={"groups": [groups_by_title[t] for t in titles]})
    return respx.post(f"{settings.TAGGER_URL}/group").mock(side_effect=reply)


@respx.mock
async def test_each_story_is_shown_once_with_the_others_under_it():
    apple1, apple2, apple3 = item("Apple unveils HomePod mini 2", "Verge"), item("HomePod mini 2 launched", "CNET"), item("Apple's new speaker", "BBC")
    other = item("Minister resigns", "RNZ")
    route = tagger_groups({apple1["title"]: 0, apple2["title"]: 0, apple3["title"]: 0, other["title"]: 1})
    views = {"all": [apple1, apple2, other, apple3], "asia": [apple2, other]}

    async with httpx.AsyncClient() as client:
        grouped = await group_stories(client, views)

    assert [i["title"] for i in grouped["all"]] == [apple1["title"], other["title"]]
    assert [(r["source"], r["title"]) for r in grouped["all"][0]["related"]] == [("CNET", apple2["title"]), ("BBC", apple3["title"])]
    # Within a view only: Asia has one Apple headline, so nothing is folded under it
    assert [i["title"] for i in grouped["asia"]] == [apple2["title"], other["title"]] and grouped["asia"][0]["related"] == []
    assert "related" not in apple2  # Views share item dicts: the originals are untouched
    # Each headline is sent once, top positions of every view first
    import json
    sent = [i["title"] for i in json.loads(route.calls.last.request.content)["items"]]
    assert sent == [apple1["title"], apple2["title"], other["title"], apple3["title"]]


@respx.mock
async def test_views_are_cut_to_the_maximum_after_folding(monkeypatch):
    monkeypatch.setattr(settings, "MAX_HEADLINES", 3)
    items = [item(f"Story {i}") for i in range(6)]
    tagger_groups({"Story 0": 0, "Story 1": 0, "Story 2": 0, "Story 3": 1, "Story 4": 2, "Story 5": 3})
    async with httpx.AsyncClient() as client:
        grouped = await group_stories(client, {"all": items})
    # Stories 1 and 2 fold into 0, so 3 and 4 move up into the first three
    assert [i["title"] for i in grouped["all"]] == ["Story 0", "Story 3", "Story 4"]


@respx.mock
async def test_ungrouped_when_the_tagger_fails(monkeypatch):
    monkeypatch.setattr(settings, "MAX_HEADLINES", 2)
    respx.post(f"{settings.TAGGER_URL}/group").mock(side_effect=httpx.ConnectError("down"))
    items = [item("A"), item("B"), item("C")]
    async with httpx.AsyncClient() as client:
        grouped = await group_stories(client, {"all": items})
    assert [i["title"] for i in grouped["all"]] == ["A", "B"] and grouped["all"][0]["related"] == []


@respx.mock
async def test_related_headlines_are_capped(monkeypatch):
    monkeypatch.setattr(settings, "MAX_RELATED", 2)
    items = [item(f"Same story {i}") for i in range(5)]
    tagger_groups({i["title"]: 0 for i in items})
    async with httpx.AsyncClient() as client:
        grouped = await group_stories(client, {"all": items})
    assert len(grouped["all"]) == 1 and len(grouped["all"][0]["related"]) == 2


@respx.mock
async def test_the_same_article_adds_no_row_but_its_discussion_is_kept():
    nyt = {**item("Worlds More Habitable Than Earth", "NYT"), "link": "https://www.nytimes.com/2026/space.html"}
    hn = {"title": "Worlds more habitable than Earth?", "link": "https://www.nytimes.com/2026/space.html#x",
          "source": "Hacker News", "comments": "https://news.ycombinator.com/item?id=1"}
    other = {"title": "Planets better for life than ours", "link": "https://bbc.example/planets", "source": "BBC",
             "comments": "https://news.ycombinator.com/item?id=2"}
    tagger_groups({nyt["title"]: 0, hn["title"]: 0, other["title"]: 0})
    async with httpx.AsyncClient() as client:
        story = (await group_stories(client, {"all": [nyt, hn, other]}))["all"][0]
    assert story["comments"] == "https://news.ycombinator.com/item?id=1"  # HN's thread, on the story itself
    assert story["related"] == [{"title": other["title"], "link": other["link"], "source": "BBC",
                                 "comments": "https://news.ycombinator.com/item?id=2"}]
    assert "comments" not in nyt  # The shared item is untouched


@respx.mock
async def test_the_same_article_is_shown_once_even_when_the_headlines_differ():
    article = {**item("Habitable worlds found", "NYT"), "link": "https://www.nytimes.com/2026/space.html"}
    hn = {"title": "Show me the planets", "link": "https://www.nytimes.com/2026/space.html/",
          "source": "Hacker News", "comments": "https://news.ycombinator.com/item?id=1"}
    respx.post(f"{settings.TAGGER_URL}/group").mock(side_effect=httpx.ConnectError("down"))  # No model groups at all
    async with httpx.AsyncClient() as client:
        grouped = (await group_stories(client, {"all": [article, hn]}))["all"]
    assert [i["title"] for i in grouped] == ["Habitable worlds found"]
    assert grouped[0]["comments"] == "https://news.ycombinator.com/item?id=1"
