"""
Searching the headline archive (services/archive.py) for the page's older
headlines, read-only.

The query is split into words as the index splits text (anything but letters
and digits is a break, so search syntax is never passed through), and every
word must start a word in the title, source or summary, case and accents
ignored, as in the page's own search. Results are stories (services/archive.py):
each once, led by its newest matching headline, with the story's other headlines
under it. Headline matches first, then the rest, each newest first: picked by
when the archive first had them (which the index follows without sorting every
match), then shown by the date published. LIMIT stories at a time: each answer's
"next" asks for the ones after them.
With SEARCH_TYPO_FALLBACK, a word of TYPO_MIN_LETTERS or more that starts no
word at all is searched as the known word one edit away (a letter deleted,
added, changed or two swapped) that the most items have.

Once the app worker has mapped the refresher's index file (services/archive_index.py),
the matches are found there, with the same results; only the results' rows come
from SQLite, over a connection the worker keeps. Before that, SQLite's full-text
index answers.
"""

import re
import sqlite3
import threading
import unicodedata
from contextlib import closing
from typing import Dict, List, Optional, Tuple

from pyroaring import BitMap

from config import settings
from services import archive_index

LIMIT = 50  # Stories per answer
MAX_RELATED = 20  # A story's other headlines listed under it
SCAN = 200  # Matches read at a time
MAX_WORDS = 10
TYPO_MIN_LETTERS = 4  # Shorter words have too many near misses
LETTERS = "abcdefghijklmnopqrstuvwxyz0123456789"

FIELDS = "link, title, source, coalesce(published, first_seen), summary, comments, story"


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


def search(query: str, after: Optional[str] = None) -> Dict:
    """{"items": up to LIMIT stories, "words": the words searched, "corrected": [{"from", "to"}],
    "next": the `after` that asks for the ones after these, or None}; `after` is an earlier answer's "next"."""
    words = words_of(query)
    index = archive_index.current()
    if index is not None:
        return _search_in_memory(index, words, after)
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
        groups, next_page = _newest(db, " ".join(terms), after) if terms else ([], None)
        ids = [i for group in groups for i in group]
        rows = {row[0]: row[1:] for row in db.execute(
            f"SELECT id, {FIELDS} FROM items WHERE id IN ({','.join('?' * len(ids))})", ids)} if ids else {}
        stories = list({rows[i][6] for i in ids} - {None})
        members: Dict[int, list] = {}
        for start in range(0, len(stories), 500):
            part = stories[start:start + 500]
            for row in db.execute(f"SELECT id, {FIELDS} FROM items WHERE story IN ({','.join('?' * len(part))}) "
                                  "ORDER BY coalesce(published, first_seen) DESC", part):
                members.setdefault(row[7], []).append(row)
    ids = [i for group in groups for i in sorted(group, key=lambda i: -rows[i][3])]  # Picked by arrival, shown by date
    keys = ("link", "title", "source", "published", "summary", "comments", "story")
    return {"items": [{
        **dict(zip(keys, rows[i])),
        "related": [{"title": m[2], "link": m[1], "source": m[3], "comments": m[6]}
                    for m in members.get(rows[i][6], []) if m[0] != i][:MAX_RELATED],
    } for i in ids], "words": used, "corrected": corrected, "next": next_page}


_kept = threading.local()


def _kept_connection() -> sqlite3.Connection:
    """This thread's connection to the archive, kept: opening one costs more than a search in memory."""
    db = getattr(_kept, "db", None)
    if db is None:
        db = _kept.db = _connect()
    return db


def _plain(word: str) -> str:
    """The word as the full-text index stores words: lower case, without accents."""
    return "".join(c for c in unicodedata.normalize("NFD", word) if not unicodedata.combining(c))


def _search_in_memory(index: "archive_index.Index", words: List[str], after: Optional[str]) -> Dict:
    """search(), its matches found in the in-memory index."""
    corrected, used, found = [], [], []
    for word in words:
        plain = _plain(word)
        anywhere, in_title = index.prefix(plain) if plain else (archive_index.EMPTY, archive_index.EMPTY)
        known = None
        if not anywhere and settings.SEARCH_TYPO_FALLBACK and len(word) >= TYPO_MIN_LETTERS:
            known = index.known(_variants(word))
            if known:
                corrected.append({"from": word, "to": known})
                anywhere, in_title = index.word(known)  # The whole word, not words starting with it
        used.append(known or word)
        found.append((anywhere, in_title))
    groups: List[List[int]] = []
    next_page = None
    if found:
        matched = BitMap.intersection(*[anywhere for anywhere, _ in found])
        titled = BitMap.intersection(*[in_title for _, in_title in found])
        groups, next_page = _newest_in(index, titled, matched - titled, after)
    leads = [i for group in groups for i in group]
    others = {i: [m for m in index.members(index.story_of(i)) if m != i][:MAX_RELATED] for i in leads}
    ids = leads + [m for ms in others.values() for m in ms]
    db = _kept_connection()
    rows = {}
    for start in range(0, len(ids), 500):
        part = ids[start:start + 500]
        rows.update((row[0], row[1:]) for row in db.execute(
            f"SELECT id, {FIELDS} FROM items WHERE id IN ({','.join('?' * len(part))})", part))
    ordered = [i for group in groups for i in sorted(group, key=lambda i: -rows[i][3])]  # Picked by arrival, shown by date
    keys = ("link", "title", "source", "published", "summary", "comments", "story")
    return {"items": [{
        **dict(zip(keys, rows[i])),
        "related": [{"title": rows[m][1], "link": rows[m][0], "source": rows[m][2], "comments": rows[m][5]} for m in others[i]],
    } for i in ordered], "words": used, "corrected": corrected, "next": next_page}


def _newest_in(index: "archive_index.Index", titled: BitMap, rest: BitMap,
               after: Optional[str]) -> Tuple[List[List[int]], Optional[str]]:
    """_newest, on the in-memory index's matches: headline matches (titled), then the rest."""
    group, below = (after or "t")[0], int(after[1:]) if after and after[1:] else None
    groups: List[List[int]] = [[], []]
    seen = set()

    def read(matches, into, below):
        end = matches.rank(below - 1) if below else len(matches)  # How many are older than `below`
        for k in range(end - 1, -1, -1):
            rowid = matches[k]
            key = index.story_of(rowid) or -rowid
            if key not in seen:
                if len(seen) == LIMIT:
                    return True, below
                seen.add(key)
                into.append(rowid)
            below = rowid
        return False, below

    if group == "t":
        more, last = read(titled, groups[0], below)
        if more:
            return groups, f"t{last}"
        below = None
    more, last = read(rest, groups[1], below)
    return groups, f"r{last or ''}" if more else None


def _newest(db: sqlite3.Connection, match: str, after: Optional[str]) -> Tuple[List[List[int]], Optional[str]]:
    """
    The stories of the newest headline matches, then of the rest, LIMIT in all, each as the id of
    its newest match: newest by arrival, which the index follows. A headline without a story is
    one of its own. And where the next ones start: "t<rowid>" (headline matches older than it),
    "r<rowid>" (the rest older than it) or "r" (the rest, from the newest); None if there are none.
    """
    group, below = (after or "t")[0], int(after[1:]) if after and after[1:] else None
    groups: List[List[int]] = [[], []]
    seen = set()

    def read(query, into, below):
        """Reads matches older than `below`, newest first, into `into` until LIMIT stories: (more to read, the last read)."""
        while True:
            rows = db.execute(
                "SELECT s.rowid, i.story FROM items_search s JOIN items i ON i.id = s.rowid "
                "WHERE items_search MATCH ? AND s.rowid < ? ORDER BY s.rowid DESC LIMIT ?",
                (query, below or 2 ** 63 - 1, SCAN)).fetchall()
            for rowid, story in rows:
                key = story if story is not None else -rowid
                if key not in seen:
                    if len(seen) == LIMIT:
                        return True, below
                    seen.add(key)
                    into.append(rowid)
                below = rowid
            if len(rows) < SCAN:
                return False, below

    if group == "t":
        more, last = read(f"{{title}} : ({match})", groups[0], below)
        if more:
            return groups, f"t{last}"
        below = None
    more, last = read(f"({match}) NOT ({{title}} : ({match}))", groups[1], below)
    return groups, f"r{last or ''}" if more else None


def _variants(word: str) -> set:
    """The words one edit away (a letter deleted, added, changed or two swapped), without accents as the index's words."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", word) if not unicodedata.combining(c))
    splits = [(plain[:i], plain[i:]) for i in range(len(plain) + 1)]
    return ({a + b[1:] for a, b in splits if b}
            | {a + b[1] + b[0] + b[2:] for a, b in splits if len(b) > 1}
            | {a + c + b[1:] for a, b in splits if b for c in LETTERS}
            | {a + c + b for a, b in splits for c in LETTERS}) - {plain}


def _correction(db: sqlite3.Connection, word: str):
    """The known word one edit away that the most items have, if the word starts no word at all; else None."""
    if len(word) < TYPO_MIN_LETTERS or db.execute(
            "SELECT 1 FROM items_search WHERE items_search MATCH ? LIMIT 1", (f'"{word}"*',)).fetchone():
        return None
    variants = _variants(word)  # The index's words have no accents
    found = db.execute(
        f"SELECT term FROM items_search_vocab WHERE term IN ({','.join('?' * len(variants))}) ORDER BY doc DESC, term LIMIT 1",
        list(variants)).fetchone()
    return found[0] if found else None
