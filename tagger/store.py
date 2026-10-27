"""
The tagger's SQLite database: every headline once (cleaned title, its feed's
label, tags, embeddings), plus the story pairs grouping was unsure of and the
judge's verdicts on them.
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
    story_embedding BLOB          -- float16[384], from the story model; made when first grouped
);
-- Pairs of (cleaned) titles that grouping was unsure of, a < b
CREATE TABLE IF NOT EXISTS story_candidates (
    a          TEXT NOT NULL,
    b          TEXT NOT NULL,
    similarity REAL NOT NULL,
    rank       INTEGER NOT NULL,  -- The later of the two's positions in the last grouping (lower: more important)
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
"""


def connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript(SCHEMA)
    if "story_embedding" not in {row[1] for row in db.execute("PRAGMA table_info(headlines)")}:
        db.execute("ALTER TABLE headlines ADD COLUMN story_embedding BLOB")  # Databases made before grouping
    return db


def to_blob(vector: np.ndarray) -> bytes:
    return vector.astype(np.float16).tobytes()


def from_blobs(blobs) -> np.ndarray:
    return np.stack([np.frombuffer(b, dtype=np.float16) for b in blobs]).astype(np.float32)
