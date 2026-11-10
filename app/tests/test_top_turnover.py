"""Tests for headlines leaving a tab's top positions after a while there (services/views/top_turnover.py)."""

import pytest

from config import settings
from services.views.top_turnover import turn_over, wear_off

NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def top(monkeypatch):
    monkeypatch.setattr(settings, "TOP_POSITIONS", 3)
    monkeypatch.setattr(settings, "TOP_DWELL_SECONDS", 7200)


def item(n, story=None):
    return {"title": f"Story {n}", "link": f"https://pub.example/{n}", "source": "Pub", **({"story": story} if story else {})}


def titles(items):
    return [int(i["title"].split()[1]) for i in items]


def test_a_headline_leaves_the_top_after_its_time_there():
    items = [item(n) for n in range(6)]
    since = {"https://pub.example/0": NOW - 7200, "https://pub.example/1": NOW - 600}
    ordered, _ = wear_off(items, since, NOW)
    # 0 has had its two hours on top: the next one moves up, and 0 keeps its place among the rest
    assert titles(ordered) == [1, 2, 3, 0, 4, 5]


def test_when_each_headline_reached_the_top_is_kept_while_it_is_in_the_tab():
    items = [item(n) for n in range(5)]
    since = {"https://pub.example/1": NOW - 600, "https://pub.example/9": NOW - 600}
    _, after = wear_off(items, since, NOW)
    # 0 and 2 just reached the top; 1 keeps its time; 9 has left the tab; 3 and 4 were never on top
    assert after == {"https://pub.example/0": NOW, "https://pub.example/1": NOW - 600, "https://pub.example/2": NOW}


def test_a_headline_that_had_its_time_on_top_keeps_it_below():
    items = [item(n) for n in range(5)]
    since = {"https://pub.example/0": NOW - 9000}
    ordered, after = wear_off(items, since, NOW)
    assert titles(ordered) == [1, 2, 3, 0, 4] and after["https://pub.example/0"] == NOW - 9000  # Not on top again later


def test_a_story_keeps_its_time_on_top_when_another_of_its_headlines_leads_it():
    since = {"story:7": NOW - 7200}
    ordered, _ = wear_off([item(0, story=7), item(1), item(2), item(3)], since, NOW)
    assert titles(ordered) == [1, 2, 3, 0]


@pytest.mark.asyncio
async def test_each_tab_remembers_its_own_top_across_refreshes(mock_redis):
    views = {"all": [item(n) for n in range(5)], "asia": [item(4), item(0)]}
    await turn_over(mock_redis, views, NOW)
    later = await turn_over(mock_redis, {"all": [item(n) for n in range(5)], "asia": [item(4), item(0)]}, NOW + 7200)
    assert titles(later["all"]) == [3, 4, 0, 1, 2]
    assert titles(later["asia"]) == [4, 0]  # Nothing fresh to move up: the order stays
