"""
The headline archive: every published headline, its link and summary, kept
forever in a SQLite file of its own (ARCHIVE_PATH), plus each view's order
once an hour, so a past front page can be shown again later. Only the
refresher writes it; the app searches it, read-only (services/archive_search.py).
"""

import json
import sqlite3
from contextlib import closing
from typing import Dict, List

from config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id         INTEGER PRIMARY KEY,
    link       TEXT NOT NULL UNIQUE,
    title      TEXT NOT NULL,      -- The latest title (feeds correct and update them)
    source     TEXT NOT NULL,
    published  REAL,               -- NULL for a headline only ever seen folded under another
    comments   TEXT,
    summary    TEXT,               -- Kept once made; the summarizer's own copy expires
    first_seen REAL NOT NULL,
    last_seen  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS items_first_seen ON items (first_seen);
CREATE INDEX IF NOT EXISTS items_published ON items (published);
-- Every title a headline has had (items.title is the latest), and when each was first seen
CREATE TABLE IF NOT EXISTS titles (
    item       INTEGER NOT NULL REFERENCES items (id),
    title      TEXT NOT NULL,
    first_seen REAL NOT NULL,
    PRIMARY KEY (item, title)
);
-- A view's headlines in display order: a JSON list with one list per story,
-- its headline's id then the ids of the headlines folded under it
CREATE TABLE IF NOT EXISTS snapshots (
    view  TEXT NOT NULL,           -- "all", a region id, or "<region>:<topic>"
    taken REAL NOT NULL,
    items TEXT NOT NULL,
    PRIMARY KEY (view, taken)
);
-- Full-text index of items' title, source and summary, for the search of older headlines
-- (services/archive_search.py): words of any case and accents, by their first 1-4 letters;
-- which column a word is in, but not where (no phrase searches, less than half the size)
CREATE VIRTUAL TABLE IF NOT EXISTS items_search USING fts5(
    title, source, summary, content=items, content_rowid=id,
    tokenize="unicode61 remove_diacritics 2", prefix="1 2 3 4", detail=column
);
-- Its words and how many items have each, for correcting typos
CREATE VIRTUAL TABLE IF NOT EXISTS items_search_vocab USING fts5vocab(items_search, 'row');
CREATE TRIGGER IF NOT EXISTS items_search_insert AFTER INSERT ON items BEGIN
    INSERT INTO items_search (rowid, title, source, summary) VALUES (new.id, new.title, new.source, new.summary);
END;
-- Every refresh rewrites each item (UPSERT), mostly unchanged: only real changes touch the index
CREATE TRIGGER IF NOT EXISTS items_search_update AFTER UPDATE OF title, source, summary ON items
WHEN old.title IS NOT new.title OR old.source IS NOT new.source OR old.summary IS NOT new.summary BEGIN
    INSERT INTO items_search (items_search, rowid, title, source, summary) VALUES ('delete', old.id, old.title, old.source, old.summary);
    INSERT INTO items_search (rowid, title, source, summary) VALUES (new.id, new.title, new.source, new.summary);
END;
"""

UPSERT = """
INSERT INTO items (link, title, source, published, comments, summary, first_seen, last_seen)
VALUES (:link, :title, :source, :published, :comments, :summary, :seen, :seen)
ON CONFLICT (link) DO UPDATE SET
    title = excluded.title,
    source = excluded.source,
    published = coalesce(items.published, excluded.published),
    comments = coalesce(excluded.comments, items.comments),
    summary = coalesce(excluded.summary, items.summary),
    last_seen = excluded.last_seen
"""


def connect(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")  # Readers (a future timeline) never block the refresher
    db.execute("PRAGMA synchronous=NORMAL")
    indexed = db.execute("SELECT 1 FROM sqlite_master WHERE name = 'items_search'").fetchone()
    db.executescript(SCHEMA)
    if not indexed:  # An archive from before search: index what it already holds
        with db:
            db.execute("INSERT INTO items_search (items_search) VALUES ('rebuild')")
    return db


def clean_title(title: str, source: str) -> str:
    """The title without the " - Publisher" Google News appends (the page drops it too), so it isn't a second title."""
    suffix = f" - {source}"
    return title[:-len(suffix)] if source and len(title) > len(suffix) and title.endswith(suffix) else title


def archive(views: Dict[str, List[Dict]], seen: float) -> None:
    """Stores every item of the published views (and those folded under them); records each view's order hourly."""
    rows = {}
    for items in views.values():
        for item in items:
            rows[item["link"]] = item
            for related in item.get("related") or []:
                rows.setdefault(related["link"], related)
    with closing(connect(settings.ARCHIVE_PATH)) as db, db:
        db.executemany(UPSERT, [{
            "link": link, "title": clean_title(item["title"], item["source"]), "source": item["source"],
            "published": item.get("published"),
            "comments": item.get("comments"), "summary": item.get("summary"), "seen": seen,
        } for link, item in rows.items()])
        ids = {}
        for start in range(0, len(rows), 500):
            links = list(rows)[start:start + 500]
            ids.update(db.execute(
                f"SELECT link, id FROM items WHERE link IN ({','.join('?' * len(links))})", links,
            ).fetchall())
        db.executemany("INSERT OR IGNORE INTO titles (item, title, first_seen) VALUES (?, ?, ?)",
                       [(ids[link], clean_title(item["title"], item["source"]), seen) for link, item in rows.items()])
        last = dict(db.execute("SELECT view, max(taken) FROM snapshots GROUP BY view").fetchall())
        db.executemany("INSERT INTO snapshots (view, taken, items) VALUES (?, ?, ?)", [
            (view, seen, json.dumps([[ids[item["link"]]] + [ids[r["link"]] for r in item.get("related") or []]
                                     for item in items], separators=(",", ":")))
            for view, items in views.items()
            if view not in last or seen - last[view] >= settings.ARCHIVE_SNAPSHOT_SECONDS
        ])
