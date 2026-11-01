"""
The worker's own SQLite file: headlines seen, stories, the red dots held and
the position in the stream. Only the worker writes it. It keeps RETENTION_DAYS,
and can be rebuilt with backfill.py. A batch is one transaction (commit).
"""

import json
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from scoring import DAY, Story
from stories import STORY_WINDOW_HOURS, StoryIndex

RETENTION_DAYS = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS headlines (
    link       TEXT PRIMARY KEY,
    story      INTEGER,            -- NULL: seen and not counted
    first_seen REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS headlines_first_seen ON headlines (first_seen);
CREATE TABLE IF NOT EXISTS stories (
    id         INTEGER PRIMARY KEY,
    first_seen REAL NOT NULL,
    stress_sum REAL NOT NULL,
    count      INTEGER NOT NULL,
    title      TEXT NOT NULL,
    link       TEXT NOT NULL,
    source     TEXT NOT NULL,
    vector     BLOB                -- float16[384]; only while headlines can still join
);
CREATE INDEX IF NOT EXISTS stories_first_seen ON stories (first_seen);
CREATE TABLE IF NOT EXISTS story_countries (
    story      INTEGER NOT NULL,
    country    TEXT NOT NULL,
    from_title INTEGER NOT NULL,
    lat        REAL,
    lon        REAL,
    PRIMARY KEY (story, country)
);
CREATE TABLE IF NOT EXISTS story_outlets (
    story  INTEGER NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (story, source)
);
CREATE TABLE IF NOT EXISTS dots (
    country TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS progress (
    name  TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()

    def last_id(self) -> str:
        row = self.db.execute("SELECT value FROM progress WHERE name = 'stream'").fetchone()
        return row[0] if row else "0"

    def set_last_id(self, entry_id: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO progress VALUES ('stream', ?)", (entry_id,))

    def seen(self, link: str) -> bool:
        return self.db.execute("SELECT 1 FROM headlines WHERE link = ?", (link,)).fetchone() is not None

    def headline_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM headlines").fetchone()[0]

    def add_headline(self, link: str, story: Optional[int], first_seen: float) -> None:
        self.db.execute("INSERT OR IGNORE INTO headlines VALUES (?, ?, ?)", (link, story, first_seen))

    def add_story(self, first_seen: float, stress: float, title: str, link: str, source: str, vector: np.ndarray) -> int:
        cursor = self.db.execute(
            "INSERT INTO stories (first_seen, stress_sum, count, title, link, source, vector) "
            "VALUES (?, ?, 1, ?, ?, ?, ?)",
            (first_seen, stress, title, link, source, vector.astype(np.float16).tobytes()),
        )
        return cursor.lastrowid

    def join_story(self, story: int, stress: float) -> None:
        self.db.execute("UPDATE stories SET stress_sum = stress_sum + ?, count = count + 1 WHERE id = ?", (stress, story))

    def add_outlet(self, story: int, source: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO story_outlets VALUES (?, ?)", (story, source))

    def add_country(self, story: int, country: str, from_title: bool, position: Optional[Tuple[float, float]]) -> None:
        lat, lon = position or (None, None)
        self.db.execute(
            "INSERT INTO story_countries VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT DO UPDATE SET from_title = MAX(from_title, excluded.from_title)",
            (story, country, int(from_title), lat, lon),
        )

    def stories_since(self, since: float) -> List[Story]:
        stories = {
            row[0]: Story(id=row[0], first_seen=row[1], stress=row[2] / row[3], title=row[4], link=row[5], source=row[6],
                          outlets=set(), countries={}, positions={})
            for row in self.db.execute(
                "SELECT id, first_seen, stress_sum, count, title, link, source FROM stories WHERE first_seen >= ?",
                (since,))
        }
        for story, source in self.db.execute(
                "SELECT o.story, o.source FROM story_outlets o JOIN stories s ON s.id = o.story WHERE s.first_seen >= ?", (since,)):
            stories[story].outlets.add(source)
        for story, country, from_title, lat, lon in self.db.execute(
                "SELECT c.story, c.country, c.from_title, c.lat, c.lon FROM story_countries c "
                "JOIN stories s ON s.id = c.story WHERE s.first_seen >= ?", (since,)):
            stories[story].countries[country] = bool(from_title)
            if lat is not None:
                stories[story].positions[country] = (lat, lon)
        return list(stories.values())

    def load_index(self, index: StoryIndex, now: float) -> None:
        for story, first_seen, vector in self.db.execute(
                "SELECT id, first_seen, vector FROM stories WHERE vector IS NOT NULL AND first_seen >= ?",
                (now - STORY_WINDOW_HOURS * 3600,)):
            index.insert(story, np.frombuffer(vector, dtype=np.float16).astype(np.float32), first_seen)

    def held_dots(self) -> Dict[str, dict]:
        return {country: json.loads(payload) for country, payload in self.db.execute("SELECT country, payload FROM dots")}

    def save_dots(self, dots: Dict[str, dict]) -> None:
        self.db.execute("DELETE FROM dots")
        self.db.executemany("INSERT INTO dots VALUES (?, ?)", [(c, json.dumps(d)) for c, d in dots.items()])

    def prune(self, now: float) -> None:
        cutoff = now - RETENTION_DAYS * DAY
        for table in ("story_countries", "story_outlets"):
            self.db.execute(f"DELETE FROM {table} WHERE story IN (SELECT id FROM stories WHERE first_seen < ?)", (cutoff,))
        self.db.execute("DELETE FROM stories WHERE first_seen < ?", (cutoff,))
        self.db.execute("DELETE FROM headlines WHERE first_seen < ?", (cutoff,))
        self.db.execute("UPDATE stories SET vector = NULL WHERE first_seen < ?", (now - STORY_WINDOW_HOURS * 3600,))
