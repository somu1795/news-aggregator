"""
Tests for the headline archive (services/archive.py): what the refresher keeps
of each published item, and each view's order once an hour.
"""

import json
import sqlite3

import pytest

from config import settings
from keys import summary_key
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


async def test_every_title_a_headline_had_is_kept(mock_redis, archive_db):
    await publish(mock_redis, {"all": [item(1)]}, 1000.0)
    await publish(mock_redis, {"all": [{**item(1), "title": "Story 1, updated"}]}, 2000.0)
    await publish(mock_redis, {"all": [{**item(1), "title": "Story 1, updated"}]}, 3000.0)

    assert archive_db().execute("SELECT title, first_seen FROM titles ORDER BY first_seen").fetchall() == [
        ("Story 1", 1000.0), ("Story 1, updated", 2000.0)]


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


async def test_google_news_publisher_suffix_is_not_a_new_title(mock_redis, archive_db):
    await publish(mock_redis, {"all": [item(1)]}, 1000.0)
    await publish(mock_redis, {"all:tech": [{**item(1), "title": "Story 1 - Pub"}]}, 1000.0)  # Same link via Google News

    db = archive_db()
    assert db.execute("SELECT title FROM items").fetchall() == [("Story 1",)]
    assert db.execute("SELECT title FROM titles").fetchall() == [("Story 1",)]


def searchable(db, query):
    return [link for (link,) in db.execute(
        "SELECT i.link FROM items_search s JOIN items i ON i.id = s.rowid WHERE items_search MATCH ? ORDER BY i.id", (query,))]


async def test_headlines_and_summaries_are_searchable_as_they_are_archived(mock_redis, archive_db):
    await mock_redis.set(summary_key("https://pub.example/1"), json.dumps({"text": "The council approved a bridge.", "model": "m"}))
    await publish(mock_redis, {"all": [item(1), {**item(2), "title": "Café owners protest"}]}, 1000.0)

    db = archive_db()
    assert searchable(db, '"bridge"') == ["https://pub.example/1"]  # Its summary
    assert searchable(db, '"cafe"*') == ["https://pub.example/2"]  # Accents ignored
    assert searchable(db, '"pu"*') == ["https://pub.example/1", "https://pub.example/2"]  # Its source


async def test_the_search_index_follows_changes_and_skips_unchanged_headlines(mock_redis, archive_db):
    await publish(mock_redis, {"all": [item(1)]}, 1000.0)
    db = archive_db()
    segments = db.execute("SELECT count(*) FROM items_search_data").fetchone()[0]
    await publish(mock_redis, {"all": [item(1)]}, 2000.0)  # Every refresh rewrites the row
    assert db.execute("SELECT count(*) FROM items_search_data").fetchone()[0] == segments  # The index wasn't touched

    await publish(mock_redis, {"all": [{**item(1), "title": "Bridge opens"}]}, 3000.0)
    assert searchable(db, '"bridge"') == ["https://pub.example/1"]
    assert searchable(db, '"story"') == []  # The old title is gone from the index


async def test_an_archive_from_before_search_is_indexed_on_first_use(archive_db):
    db = archive_db()
    db.executescript(archive_module.SCHEMA.split("CREATE VIRTUAL TABLE")[0])  # The tables as they were
    db.execute("INSERT INTO items (link, title, source, first_seen, last_seen) VALUES ('https://pub.example/1', 'Old story', 'Pub', 1, 1)")
    db.commit()

    archive_module.connect(settings.ARCHIVE_PATH).close()
    assert searchable(archive_db(), '"old"') == ["https://pub.example/1"]


def stories(db):
    return dict(db.execute("SELECT link, story FROM items"))


async def test_each_headline_is_archived_with_its_story(mock_redis, archive_db):
    related = {"title": "Other take", "link": "https://other.example/1", "source": "Other"}
    await publish(mock_redis, {"all": [item(1, story=7, related=[related]), item(2)]}, 1000.0)
    assert stories(archive_db()) == {"https://pub.example/1": 7, "https://other.example/1": 7, "https://pub.example/2": None}
    await publish(mock_redis, {"all": [item(1), item(2, story=9)]}, 2000.0)  # Ungrouped this time (the tagger was down)
    assert stories(archive_db()) == {"https://pub.example/1": 7, "https://other.example/1": 7, "https://pub.example/2": 9}


async def test_headlines_without_a_story_are_listed_first_archived_first_and_given_one(mock_redis, archive_db):
    await publish(mock_redis, {"all": [item(2), item(1, story=5)]}, 1000.0)
    await publish(mock_redis, {"all": [item(3)]}, 2000.0)
    missing = archive_module.without_story(10)
    assert [title for _, title, _ in missing] == ["Story 2", "Story 3"]
    archive_module.set_stories([(missing[0][0], 8)])
    assert [title for _, title, _ in archive_module.without_story(10)] == ["Story 3"]
    assert stories(archive_db())["https://pub.example/2"] == 8


async def test_the_taggers_story_changes_are_followed(mock_redis, archive_db):
    await publish(mock_redis, {"all": [item(1, story=1), item(2, story=2), item(3, story=2),
                                       {**item(4, story=2), "title": "Story  4 - Pub"}]}, 1000.0)
    assert archive_module.last_story_move() == 0
    archive_module.apply_story_moves([{"id": 3, "old": 2, "new": 1, "title": None},     # Story 2 merged into 1
                                      {"id": 4, "old": 1, "new": 4, "title": "Story 4"}])  # Then one headline moved out
    assert stories(archive_db()) == {"https://pub.example/1": 1, "https://pub.example/2": 1,
                                     "https://pub.example/3": 1, "https://pub.example/4": 4}
    assert archive_module.last_story_move() == 4


async def test_an_archive_from_before_stories_gets_them(archive_db):
    db = archive_db()
    db.execute("""CREATE TABLE items (id INTEGER PRIMARY KEY, link TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
                  source TEXT NOT NULL, published REAL, comments TEXT, summary TEXT, first_seen REAL NOT NULL,
                  last_seen REAL NOT NULL)""")  # As it was
    db.execute("INSERT INTO items (link, title, source, first_seen, last_seen) VALUES ('https://pub.example/1', 'Old story', 'Pub', 1, 1)")
    db.commit()

    assert [title for _, title, _ in archive_module.without_story(10)] == ["Old story"]
    assert searchable(archive_db(), '"old"') == ["https://pub.example/1"]  # And it was indexed for search
