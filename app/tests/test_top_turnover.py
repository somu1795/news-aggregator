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


async def test_each_tab_remembers_its_own_top_across_refreshes(mock_redis):
    views = {"all": [item(n) for n in range(5)], "asia": [item(4), item(0)]}
    await turn_over(mock_redis, views, NOW)
    later = await turn_over(mock_redis, {"all": [item(n) for n in range(5)], "asia": [item(4), item(0)]}, NOW + 7200)
    assert titles(later["all"]) == [3, 4, 0, 1, 2]
    assert titles(later["asia"]) == [4, 0]  # Nothing fresh to move up: the order stays


def extra(n):
    return {"title": f"Story {n}", "link": f"https://extra.example/{n}", "source": "Extra"}


def spaced(items, since=None):
    extras = {i["link"] for i in items if "extra.example" in i["link"]}
    return wear_off(items, since or {}, NOW, extras)


@pytest.fixture
def top_of_six(monkeypatch):
    monkeypatch.setattr(settings, "TOP_POSITIONS", 6)
    monkeypatch.setattr(settings, "TOP_MAX_EXTRAS", 2)


def test_news_leads_and_extras_are_never_side_by_side_on_top(top_of_six):
    items = [extra(0), extra(1), item(2), extra(3), item(4), item(5), item(6), item(7)]
    ordered, _ = spaced(items)
    # 0 waits for a news item to lead, 1 for one to come between; 3 would be a third extra on top
    assert titles(ordered) == [2, 0, 4, 1, 5, 6, 3, 7]


def test_extras_beyond_the_top_keep_the_mix_order(top_of_six):
    items = [item(n) for n in range(6)] + [extra(6), extra(7), item(8)]
    assert titles(spaced(items)[0]) == [0, 1, 2, 3, 4, 5, 6, 7, 8]


def test_extras_fill_the_top_when_there_is_no_news_left_for_it(top_of_six):
    items = [extra(0), extra(1), extra(2), item(3)]
    assert titles(spaced(items)[0]) == [3, 0, 1, 2]


def test_a_headline_moved_up_between_extras_starts_its_time_on_top(top_of_six):
    items = [item(0), extra(1), extra(2), extra(3)] + [item(n) for n in range(4, 10)]
    ordered, after = spaced(items)
    assert titles(ordered) == [0, 1, 4, 2, 5, 6, 3, 7, 8, 9]
    assert set(after) == {i["link"] for i in ordered[:6]}  # 3 isn't on top, so its time hasn't started


def test_spacing_and_turnover_work_together(top_of_six):
    items = [item(0), extra(1), item(2), extra(3), item(4), item(5), item(6), item(7), item(8)]
    ordered, _ = spaced(items, {"https://pub.example/0": NOW - 7200, "https://pub.example/2": NOW - 7200})
    # 0 and 2 have had their time: 4 leads, and 1 and 3 still need news between them
    assert titles(ordered) == [4, 1, 5, 3, 6, 7, 0, 2, 8]


async def test_a_tab_spaces_the_extras_it_is_told_about(mock_redis, top_of_six):
    items = [extra(0), extra(1), item(2), item(3)]
    result = await turn_over(mock_redis, {"all": items}, NOW, {i["link"] for i in items[:2]})
    assert titles(result["all"]) == [2, 0, 3, 1]
