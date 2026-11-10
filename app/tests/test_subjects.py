"""Tests for capping how many headlines about one subject a topic tab shows (services/subjects.py)."""

import json

import pytest

from config import settings
from keys import subject_key
from services.subjects import cap_subjects, normalize_subject, subjects_of


def item(n, title=None):
    return {"title": title or f"Story {n}", "link": f"https://pub.example/{n}", "source": "Pub"}


def test_subject_names_are_compared_loosely():
    assert normalize_subject("GTA VI") == normalize_subject("GTA 6") == normalize_subject(" gta-6. ")
    assert normalize_subject("The Ashes") == normalize_subject("Ashes")
    assert normalize_subject("X") == "x"  # A lone letter isn't a roman numeral
    assert normalize_subject("Henry VIII") == "henry 8"


def test_a_tab_keeps_at_most_a_few_headlines_about_one_subject(monkeypatch):
    monkeypatch.setattr(settings, "SUBJECT_CAP", 2)
    items = [item(n) for n in range(6)]
    subject_of = {items[n]["link"]: "gta 6" for n in (0, 1, 3, 5)}
    views, dropped = cap_subjects({"all:tech": items, "asia:tech": [items[3], items[4]]}, subject_of)
    # The first two keep their places; the later ones go and the rest move up
    assert [i["title"] for i in views["all:tech"]] == ["Story 0", "Story 1", "Story 2", "Story 4"]
    assert [i["title"] for i in views["asia:tech"]] == ["Story 3", "Story 4"]  # Each tab on its own
    assert [i["title"] for i in dropped] == ["Story 5"]  # Shown in no tab (Story 3 still is in Asia's)


def test_headlines_without_a_subject_are_never_capped(monkeypatch):
    monkeypatch.setattr(settings, "SUBJECT_CAP", 1)
    items = [item(n) for n in range(3)]
    views, dropped = cap_subjects({"all:tech": items}, {})
    assert views["all:tech"] == items and dropped == []


@pytest.mark.asyncio
async def test_subjects_are_read_from_the_summarizers_store(mock_redis):
    named, unnamed, blank = item(1), item(2), item(3)
    await mock_redis.set(subject_key(named["link"]), json.dumps({"subject": "GTA VI"}))
    await mock_redis.set(subject_key(blank["link"]), json.dumps({"subject": ""}))  # The LLM gave none
    assert await subjects_of(mock_redis, {"all:tech": [named, unnamed, blank]}) == {named["link"]: "gta 6"}
