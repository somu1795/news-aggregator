"""
Tests for mix_headlines: weighted interleaving, feed order, freshness,
de-duplication, per-publisher cap, and redistribution of unused slots.
"""

import pytest

from config import NewsSource, settings
from services.feed_service import mix_headlines

NOW = 1_800_000_000.0
WORLD, SCIENCE, HN = "World", "Science", "Hacker News"
SOURCES = [NewsSource(name=WORLD, url="w", weight=0.5), NewsSource(name=SCIENCE, url="s", weight=0.3), NewsSource(name=HN, url="h", weight=0.2)]


def google_item(prefix, i, publisher=None, age_hours=1.0, title=None):
    publisher = publisher or f"{prefix} Publisher {i}"
    return {
        "title": title or f"{prefix} story {i} - {publisher}",
        "link": f"https://news.google.com/rss/articles/{prefix}-{i}",
        "source": publisher,
        "published": NOW - age_hours * 3600,
    }


def hn_item(i, domain=None, age_hours=1.0, title=None):
    return {
        "title": title or f"HN story {i}",
        "link": f"https://{domain or f'site{i}.example'}/post/{i}",
        "source": HN,
        "published": NOW - age_hours * 3600,
    }


def feeds(n=60):
    return {
        WORLD: [google_item("w", i) for i in range(n)],
        SCIENCE: [google_item("s", i) for i in range(n)],
        HN: [hn_item(i) for i in range(n)],
    }


@pytest.fixture(autouse=True)
def limits(monkeypatch):
    monkeypatch.setattr(settings, "MAX_HEADLINES", 50)
    monkeypatch.setattr(settings, "MAX_HEADLINE_AGE_HOURS", 48)
    monkeypatch.setattr(settings, "MAX_PER_PUBLISHER", 3)


def source_of(item):
    return HN if item["source"] == HN else (WORLD if item["link"].split("/")[-1].startswith("w") else SCIENCE)


def test_weights_are_respected_and_interleaved():
    mixed = mix_headlines(SOURCES, feeds(), NOW)
    counts = {name: sum(1 for h in mixed if source_of(h) == name) for name in (WORLD, SCIENCE, HN)}
    assert counts == {WORLD: 25, SCIENCE: 15, HN: 10}
    # Every stretch of 10 holds the mix, instead of blocks per source
    for start in range(0, 50, 10):
        window = [source_of(h) for h in mixed[start:start + 10]]
        assert window.count(WORLD) == 5 and window.count(SCIENCE) == 3 and window.count(HN) == 2


def test_each_source_keeps_its_feed_order():
    """Feed order (e.g. HN rank) is kept, even when it isn't newest-first."""
    data = feeds()
    data[HN] = [hn_item(i, age_hours=10 - i) for i in range(10)]  # Rank 0 is the oldest
    mixed = mix_headlines(SOURCES, data, NOW)
    hn_titles = [h["title"] for h in mixed if source_of(h) == HN]
    assert hn_titles == [f"HN story {i}" for i in range(10)]


def test_order_is_deterministic():
    assert mix_headlines(SOURCES, feeds(), NOW) == mix_headlines(SOURCES, feeds(), NOW)


def test_stale_items_are_dropped():
    data = feeds()
    data[WORLD] = [google_item("w", i, age_hours=72 if i % 2 else 1) for i in range(60)]
    mixed = mix_headlines(SOURCES, data, NOW)
    assert all(NOW - h["published"] <= 48 * 3600 for h in mixed)


def test_duplicates_across_sources_are_dropped():
    """Same story via Google (with " - Publisher" suffix) and HN: first one wins."""
    data = feeds()
    data[WORLD][0] = google_item("w", 0, publisher="The Verge", title="Big Launch Happens Today - The Verge")
    data[HN][0] = hn_item(0, title="Big launch happens today")
    data[SCIENCE][0] = dict(data[WORLD][0])  # Exact same link
    mixed = mix_headlines(SOURCES, data, NOW)
    assert sum(1 for h in mixed if "launch" in h["title"].lower()) == 1


def test_publisher_cap():
    data = feeds()
    data[WORLD] = [google_item("w", i, publisher="UA.NEWS") for i in range(5)] + data[WORLD][5:]
    data[HN] = [hn_item(i, domain="github.com") for i in range(5)] + data[HN][5:]
    mixed = mix_headlines(SOURCES, data, NOW)
    assert sum(1 for h in mixed if h["source"] == "UA.NEWS") == 3
    assert sum(1 for h in mixed if "github.com" in h["link"]) == 3
    assert len(mixed) == 50


def test_short_source_slots_go_to_others():
    data = feeds()
    data[HN] = [hn_item(i) for i in range(2)]
    mixed = mix_headlines(SOURCES, data, NOW)
    assert len(mixed) == 50
    assert sum(1 for h in mixed if source_of(h) == HN) == 2


def test_empty_and_missing_sources():
    assert mix_headlines(SOURCES, {}, NOW) == []
    only_world = mix_headlines(SOURCES, {WORLD: [google_item("w", i) for i in range(5)]}, NOW)
    assert len(only_world) == 5
