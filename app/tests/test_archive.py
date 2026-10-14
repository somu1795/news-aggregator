"""
Tests for the headline archive (services/archive.py): what the refresher keeps
of each published item, and each view's order once an hour.
"""

import json
import sqlite3

import pytest

from config import settings, summary_key
from refresher import publish
from services import archive as archive_module

pytestmark = pytest.mark.asyncio


def item(n, **extra):
    return {"title": f"Story {n}", "link": f"https://pub.example/{n}", "source": "Pub",
            "published": 100.0 + n, "last_updated": 100.0 + n, **extra}


@pytest.fixture
def archive_db(tmp_path, monkeypatch):
    path = tmp_path / "archive.db"
    monkeypatch.setattr(settings, "ARCHIVE_PATH", str(path))
    return lambda: sqlite3.connect(path)


def rows(db):
    return {link: (title, published, summary, first, last) for link, title, published, summary, first, last in db.execute(
        "SELECT link, title, published, summary, first_seen, last_seen FROM items")}


async def test_published_items_are_archived_with_their_summaries(mock_redis, archive_db):
    await mock_redis.set(summary_key("https://pub.example/1"), json.dumps({"text": "A summary.", "model": "m"}))
    related = {"title": "Other take", "link": "https://other.example/1", "source": "Other"}
    await publish(mock_redis, {"all": [item(1, related=[related]), item(2)], "asia": [item(2)]}, 1000.0)

    assert rows(archive_db()) == {
        "https://pub.example/1": ("Story 1", 101.0, "A summary.", 1000.0, 1000.0),
        "https://pub.example/2": ("Story 2", 102.0, None, 1000.0, 1000.0),
        "https://other.example/1": ("Other take", None, None, 1000.0, 1000.0),  # Folded under story 1
    }


async def test_a_headline_is_stored_once_and_keeps_its_summary(mock_redis, archive_db):
    await mock_redis.set(summary_key("https://pub.example/1"), json.dumps({"text": "A summary.", "model": "m"}))
    await publish(mock_redis, {"all": [item(1)]}, 1000.0)
    await mock_redis.delete(summary_key("https://pub.example/1"))  # The summarizer's copy expired
    await publish(mock_redis, {"all": [{**item(1), "title": "Story 1, updated"}]}, 2000.0)

    assert rows(archive_db()) == {"https://pub.example/1": ("Story 1, updated", 101.0, "A summary.", 1000.0, 2000.0)}


async def test_each_view_is_recorded_hourly_in_display_order(mock_redis, archive_db):
    related = {"title": "Other take", "link": "https://other.example/1", "source": "Other"}
    await publish(mock_redis, {"all": [item(2), item(1, related=[related])]}, 1000.0)
    await publish(mock_redis, {"all": [item(1)], "asia": [item(1)]}, 1000.0 + 300)  # "all" not due yet
    await publish(mock_redis, {"all": [item(1)]}, 1000.0 + settings.ARCHIVE_SNAPSHOT_SECONDS)

    db = archive_db()
    ids = dict(db.execute("SELECT link, id FROM items"))
    snapshots = [(view, taken, json.loads(order)) for view, taken, order in
                 db.execute("SELECT view, taken, items FROM snapshots ORDER BY taken, view")]
    one, two, other = ids["https://pub.example/1"], ids["https://pub.example/2"], ids["https://other.example/1"]
    assert snapshots == [
        ("all", 1000.0, [[two], [one, other]]),
        ("asia", 1300.0, [[one]]),
        ("all", 4600.0, [[one]]),
    ]


async def test_an_archive_failure_does_not_stop_publishing(mock_redis, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "ARCHIVE_PATH", str(tmp_path / "missing-dir" / "archive.db"))
    await publish(mock_redis, {"all": [item(1)]}, 1000.0)
    assert await mock_redis.get("headlines:all")


async def test_nothing_is_archived_without_a_path(mock_redis, monkeypatch):
    called = []
    monkeypatch.setattr(archive_module, "connect", lambda path: called.append(path))
    await publish(mock_redis, {"all": [item(1)]}, 1000.0)
    assert called == []
