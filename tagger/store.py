"""
The tagger's SQLite database: every headline once (cleaned title, its feed's
label, tags, embeddings, story), plus the story pairs grouping was unsure of,
the judge's verdicts on them and the story changes they made.
"""

import re
import sqlite3
from pathlib import Path
from typing import Optional

import numpy as np

def clean_title(title: str, source: Optional[str] = None) -> str:
    """Drops Google's " - Publisher" suffix and normalizes whitespace (as in the training data)."""
    if source and title.endswith(" - " + source):
        title = title[: -len(source) - 3]
    return re.sub(r"\s+", " ", title).strip()


SCHEMA = """
CREATE TABLE IF NOT EXISTS headlines (
    title      TEXT PRIMARY KEY,  -- Cleaned title
    source     TEXT,
    link       TEXT,
    label      TEXT,              -- Implied by its feed: a topic, "other", or NULL
    tags       TEXT,              -- JSON list, from classifier `model`
    model      TEXT,
    embedding  BLOB NOT NULL,     -- float16[384]
    first_seen REAL NOT NULL,
    story_embedding BLOB,         -- float16[384], from the story model; made when first grouped
    story      INTEGER            -- Its story: the rowid of the story's first headline; given when first grouped
);
-- Pairs of (cleaned) titles that grouping was unsure of, a < b
CREATE TABLE IF NOT EXISTS story_candidates (
    a          TEXT NOT NULL,
    b          TEXT NOT NULL,
    similarity REAL NOT NULL,
    rank       INTEGER NOT NULL,  -- Its headline's position in the last grouping (lower: more important)
    seen       REAL NOT NULL,
    PRIMARY KEY (a, b)
);
-- A judge's decision on a pair: the same story or not; overrides their similarity
CREATE TABLE IF NOT EXISTS story_verdicts (
    a       TEXT NOT NULL,
    b       TEXT NOT NULL,
    same    INTEGER NOT NULL,
    model   TEXT,
    decided REAL NOT NULL,
    PRIMARY KEY (a, b)
);
-- Stories changed by verdicts, for copies of them (the archive) to follow: a whole story
-- merged into another (title NULL), or one headline moved out to a story of its own
CREATE TABLE IF NOT EXISTS story_moves (
    id    INTEGER PRIMARY KEY,
    old   INTEGER NOT NULL,
    new   INTEGER NOT NULL,
    title TEXT,
    moved REAL NOT NULL
);
"""
# After the columns they need, which databases made before them get in connect()
INDEXES = """
CREATE INDEX IF NOT EXISTS headlines_first_seen ON headlines (first_seen);
CREATE INDEX IF NOT EXISTS headlines_story ON headlines (story);
CREATE INDEX IF NOT EXISTS story_verdicts_b ON story_verdicts (b);
"""


def connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript(SCHEMA)
    columns = {row[1] for row in db.execute("PRAGMA table_info(headlines)")}
    if "story_embedding" not in columns:
        db.execute("ALTER TABLE headlines ADD COLUMN story_embedding BLOB")  # Databases made before grouping
    if "story" not in columns:
        db.execute("ALTER TABLE headlines ADD COLUMN story INTEGER")  # ...or before lasting stories
    db.executescript(INDEXES)
    return db


def to_blob(vector: np.ndarray) -> bytes:
    return vector.astype(np.float16).tobytes()


def from_blobs(blobs) -> np.ndarray:
    return np.stack([np.frombuffer(b, dtype=np.float16) for b in blobs]).astype(np.float32)
