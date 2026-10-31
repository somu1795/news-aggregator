"""
Searching the headline archive (services/archive.py) for the page's older
headlines, read-only.

The query is split into words as the index splits text (anything but letters
and digits is a break, so search syntax is never passed through), and every
word must start a word in the title, source or summary, case and accents
ignored, as in the page's own search. Results:
- "newest": headline matches first, then the rest, each newest first: picked
  by when the archive first had them (which the index follows without sorting
  every match), then shown by the date published;
- "best": the newest BEST_POOL matches ranked by BM25, a word in the title
  counting most. Ranking every match of a common word would take seconds.
With SEARCH_TYPO_FALLBACK, a word of TYPO_MIN_LETTERS or more that starts no
word at all is searched as the known word one edit away (a letter deleted,
added, changed or two swapped) that the most items have.
"""

import re
import sqlite3
import unicodedata
from contextlib import closing
from typing import Dict, List

from config import settings

LIMIT = 50
BEST_POOL = 5000  # Measured on a year-sized copy: ~70 ms at most; all matches of "a" took 2.7 s
MAX_WORDS = 10
TYPO_MIN_LETTERS = 4  # Shorter words have too many near misses
LETTERS = "abcdefghijklmnopqrstuvwxyz0123456789"

FIELDS = "link, title, source, coalesce(published, first_seen), summary, comments"


class ArchiveUnavailable(Exception):
    pass


def _connect() -> sqlite3.Connection:
    if not settings.ARCHIVE_PATH:
        raise ArchiveUnavailable("no archive")
    try:
        db = sqlite3.connect(f"file:{settings.ARCHIVE_PATH}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        raise ArchiveUnavailable("no archive")
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'items_search'").fetchone():
        db.close()
        raise ArchiveUnavailable("archive not indexed yet")  # The refresher indexes it when it next writes
    return db


def words_of(query: str) -> List[str]:
    return re.findall(r"[^\W_]+", query.lower())[:MAX_WORDS]


def search(query: str, sort: str) -> Dict:
    """{"items": up to LIMIT archived items, "words": the words searched, "corrected": [{"from", "to"}]}."""
    words = words_of(query)
    with closing(_connect()) as db:
        corrected, terms, used = [], [], []
        for word in words:
            known = _correction(db, word) if settings.SEARCH_TYPO_FALLBACK else None
            if known:
                corrected.append({"from": word, "to": known})
                terms.append(f'"{known}"')  # The whole word, not words starting with it
            else:
                terms.append(f'"{word}"*')
            used.append(known or word)
        groups = (_newest(db, " ".join(terms)) if sort == "newest" else [_best(db, " ".join(terms))]) if terms else []
        ids = [i for group in groups for i in group]
        rows = {row[0]: row[1:] for row in db.execute(
            f"SELECT id, {FIELDS} FROM items WHERE id IN ({','.join('?' * len(ids))})", ids)} if ids else {}
    if sort == "newest":  # Picked by arrival, shown by the date the page shows
        ids = [i for group in groups for i in sorted(group, key=lambda i: -rows[i][3])]
    keys = ("link", "title", "source", "published", "summary", "comments")
    return {"items": [dict(zip(keys, rows[i])) for i in ids], "words": used, "corrected": corrected}


def _newest(db: sqlite3.Connection, match: str) -> List[List[int]]:
    """The newest headline matches, then the rest, LIMIT in all: newest by arrival, which the index follows."""
    in_title = [i for (i,) in db.execute(
        "SELECT rowid FROM items_search WHERE items_search MATCH ? ORDER BY rowid DESC LIMIT ?",
        (f"{{title}} : ({match})", LIMIT))]
    if len(in_title) == LIMIT:
        return [in_title]
    rest = db.execute(
        f"SELECT rowid FROM items_search WHERE items_search MATCH ? AND rowid NOT IN ({','.join('?' * len(in_title)) or 'NULL'})"
        " ORDER BY rowid DESC LIMIT ?", (match, *in_title, LIMIT - len(in_title)))
    return [in_title, [i for (i,) in rest]]


def _best(db: sqlite3.Connection, match: str) -> List[int]:
    return [i for (i,) in db.execute(
        "SELECT rowid FROM (SELECT rowid, bm25(items_search, 10.0, 2.0, 1.0) AS score FROM items_search"
        " WHERE items_search MATCH ? ORDER BY rowid DESC LIMIT ?) ORDER BY score, rowid DESC LIMIT ?",
        (match, BEST_POOL, LIMIT))]


def _correction(db: sqlite3.Connection, word: str):
    """The known word one edit away that the most items have, if the word starts no word at all; else None."""
    if len(word) < TYPO_MIN_LETTERS or db.execute(
            "SELECT 1 FROM items_search WHERE items_search MATCH ? LIMIT 1", (f'"{word}"*',)).fetchone():
        return None
    # The index's words have no accents
    plain = "".join(c for c in unicodedata.normalize("NFKD", word) if not unicodedata.combining(c))
    splits = [(plain[:i], plain[i:]) for i in range(len(plain) + 1)]
    variants = ({a + b[1:] for a, b in splits if b}
                | {a + b[1] + b[0] + b[2:] for a, b in splits if len(b) > 1}
                | {a + c + b[1:] for a, b in splits if b for c in LETTERS}
                | {a + c + b for a, b in splits for c in LETTERS}) - {plain}
    found = db.execute(
        f"SELECT term FROM items_search_vocab WHERE term IN ({','.join('?' * len(variants))}) ORDER BY doc DESC, term LIMIT 1",
        list(variants)).fetchone()
    return found[0] if found else None
