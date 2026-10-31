"""
The archive's search index: built once per refresh cycle by the refresher, into a
file next to the archive (INDEX_NAME), which every app worker maps into memory. A
search of older headlines (services/archive_search.py) then finds its matches without
asking SQLite, and the workers share one copy of the index (the system's file cache).

The file holds, for every word of the archive's titles, sources and summaries, and for
every 1-3 letter prefix of them: the headlines that have it, and those that have it in
their title, as compressed bitmaps of their ids (roaring bitmaps, whose AND and OR work
on whole blocks with AVX2). Plus each headline's story, and each story's headlines,
newest first. Words come from SQLite's own full-text index (fts5vocab), so they are
split and folded exactly as SQLite does it.

The refresher writes a new file and renames it over the old one (write()); a worker
checks for a new one every CHECK_SECONDS and maps it (load()), while searches already
running finish on the old one. A bitmap is read from the file when a search needs it
(~1 µs); a worker keeps only the word list in its own memory. Until there is a file,
searches use SQLite alone.
"""

import bisect
import ctypes
import json
import logging
import mmap
import os
import sqlite3
import struct
import threading
import time
from array import array
from typing import Dict, List, Optional, Tuple

from pyroaring import BitMap

from config import settings

logger = logging.getLogger(__name__)

INDEX_NAME = "search-index.bin"  # Next to ARCHIVE_PATH
MAGIC = b"NAINDEX1"
PREFIX_LETTERS = 3  # Prefixes up to this long are stored; a longer one unions its few words
CHECK_SECONDS = 10
EMPTY = BitMap()


def index_path() -> str:
    return os.path.join(os.path.dirname(settings.ARCHIVE_PATH), INDEX_NAME)


# --- Writing (the refresher) ---

def write() -> Dict:
    """Builds the index from the archive and puts it in place of the previous one; returns what it measured."""
    started = time.monotonic()
    db = sqlite3.connect(f"file:{settings.ARCHIVE_PATH}?mode=ro", uri=True)
    try:
        sections, headlines, words = _build(db)
    finally:
        db.close()
    path = index_path()
    try:
        with open(path + ".tmp", "wb") as f:
            size = _write_sections(f, sections, {"headlines": headlines})
        os.replace(path + ".tmp", path)
    except BaseException:
        if os.path.exists(path + ".tmp"):  # A disk filled up midway: don't leave 20 MB behind
            os.remove(path + ".tmp")
        raise
    try:  # The build's working memory goes back to the system, not to the allocator's free lists
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass
    return {"headlines": headlines, "words": words, "mb": round(size / 2**20, 1), "seconds": round(time.monotonic() - started, 2)}


def _build(db: sqlite3.Connection):
    words: List[str] = []
    all_docs: List[BitMap] = []
    title_docs: List[BitMap] = []
    db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.words USING fts5vocab(main, items_search, 'instance')")
    term, docs, titles = None, [], []

    def add():
        if term is not None:
            words.append(term)
            all_docs.append(BitMap(docs))
            title_docs.append(BitMap(titles))
    for word, doc, column in db.execute("SELECT term, doc, col FROM temp.words"):  # In word order
        if word != term:
            add()
            term, docs, titles = word, [], []
        docs.append(doc)
        if column == "title":
            titles.append(doc)
    add()
    groups: Dict[str, List[int]] = {}
    for i, word in enumerate(words):
        for n in range(1, min(PREFIX_LETTERS, len(word)) + 1):
            groups.setdefault(word[:n], []).append(i)
    prefixes = sorted(groups)
    rows = db.execute("SELECT id, story, coalesce(published, first_seen) FROM items").fetchall()
    max_id = max((r[0] for r in rows), default=0)
    story = array("q", bytes(8 * (max_id + 1)))  # By id; 0: none
    members: Dict[int, List[int]] = {}
    for i, s, _ in sorted(rows, key=lambda r: (-r[2], r[0])):  # Newest first
        if s is not None:
            story[i] = s
            members.setdefault(s, []).append(i)
    keys = sorted(members)
    starts = array("Q", [0])
    for s in keys:
        starts.append(starts[-1] + len(members[s]))
    sections = {
        "words": "\n".join(words).encode(),
        "counts": array("Q", [len(b) for b in all_docs]).tobytes(),
        **_bitmaps("word_all", all_docs),
        **_bitmaps("word_title", title_docs),
        "prefixes": "\n".join(prefixes).encode(),
        **_bitmaps("prefix_all", [BitMap.union(EMPTY, *[all_docs[i] for i in groups[p]]) for p in prefixes]),
        **_bitmaps("prefix_title", [BitMap.union(EMPTY, *[title_docs[i] for i in groups[p]]) for p in prefixes]),
        "story": story.tobytes(),
        "story_keys": array("q", keys).tobytes(),
        "story_starts": starts.tobytes(),
        "story_members": array("Q", [i for s in keys for i in members[s]]).tobytes(),
    }
    return sections, len(rows), len(words)


def _bitmaps(name: str, bitmaps: List[BitMap]) -> Dict[str, bytes]:
    """The bitmaps, serialized one after another, and where each starts (one more offset: the end)."""
    blobs = [b.serialize() if b else b"" for b in bitmaps]
    offsets = array("Q", [0])
    for blob in blobs:
        offsets.append(offsets[-1] + len(blob))
    return {name: b"".join(blobs), name + "_at": offsets.tobytes()}


def _write_sections(f, sections: Dict[str, bytes], meta: Dict) -> int:
    """
    MAGIC, the length of a JSON header, the header (each section's offset and length), the
    sections 8-byte aligned, then MAGIC reversed: a file that doesn't end with it was cut short.
    """
    places, offset = {}, 0
    for name, data in sections.items():
        places[name] = [offset, len(data)]
        offset += (len(data) + 7) // 8 * 8
    header = json.dumps({**meta, "sections": places}).encode()
    start = (len(MAGIC) + 4 + len(header) + 7) // 8 * 8
    f.write(MAGIC + struct.pack("<I", len(header)) + header + b"\0" * (start - len(MAGIC) - 4 - len(header)))
    for data in sections.values():
        f.write(data + b"\0" * ((len(data) + 7) // 8 * 8 - len(data)))
    f.write(MAGIC[::-1])  # Last: a file that ends without it was cut short
    return start + offset + 8  # The end marker


# --- Reading (the app's workers) ---

class Index:
    """One index file, mapped into memory: read-only, so a search needs no lock."""

    def __init__(self, path: str) -> None:
        with open(path, "rb") as f:
            self._map = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        view = memoryview(self._map)
        if bytes(view[:len(MAGIC)]) != MAGIC:
            raise ValueError("not a search index file")
        length = struct.unpack("<I", view[len(MAGIC):len(MAGIC) + 4])[0]
        meta = json.loads(bytes(view[len(MAGIC) + 4:len(MAGIC) + 4 + length]))
        start = (len(MAGIC) + 4 + length + 7) // 8 * 8
        end = start + sum((size + 7) // 8 * 8 for _, size in meta["sections"].values())
        if len(view) != end + 8 or bytes(view[end:]) != MAGIC[::-1]:
            raise ValueError("search index file cut short or damaged")

        def section(name, fmt=None):
            offset, size = meta["sections"][name]
            part = view[start + offset:start + offset + size]
            return part.cast(fmt) if fmt else part
        words = bytes(section("words")).decode()
        self.words = words.split("\n") if words else []
        self.position = {w: i for i, w in enumerate(self.words)}
        self.counts = section("counts", "Q")
        prefixes = bytes(section("prefixes")).decode()
        self.prefixes = {p: i for i, p in enumerate(prefixes.split("\n"))} if prefixes else {}
        self._sets = {name: (section(name), section(name + "_at", "Q"))
                      for name in ("word_all", "word_title", "prefix_all", "prefix_title")}
        self._story = section("story", "q")
        self._story_keys = section("story_keys", "q")
        self._story_starts = section("story_starts", "Q")
        self._story_members = section("story_members", "Q")
        self.headlines = meta["headlines"]

    def _bitmap(self, name: str, i: int) -> BitMap:
        data, at = self._sets[name]
        return BitMap.deserialize(data[at[i]:at[i + 1]]) if at[i + 1] > at[i] else EMPTY

    def prefix(self, start: str) -> Tuple[BitMap, BitMap]:
        """The headlines with a word starting with `start`: anywhere, and in their title."""
        if len(start) <= PREFIX_LETTERS:
            i = self.prefixes.get(start)
            return (EMPTY, EMPTY) if i is None else (self._bitmap("prefix_all", i), self._bitmap("prefix_title", i))
        lo = bisect.bisect_left(self.words, start)
        hi = bisect.bisect_left(self.words, start[:-1] + chr(ord(start[-1]) + 1), lo)
        return (BitMap.union(EMPTY, *[self._bitmap("word_all", i) for i in range(lo, hi)]),
                BitMap.union(EMPTY, *[self._bitmap("word_title", i) for i in range(lo, hi)]))

    def word(self, word: str) -> Tuple[BitMap, BitMap]:
        """The headlines with exactly this word: anywhere, and in their title."""
        i = self.position.get(word)
        return (EMPTY, EMPTY) if i is None else (self._bitmap("word_all", i), self._bitmap("word_title", i))

    def known(self, words) -> Optional[str]:
        """Of these words, the one the most headlines have (ties: the first in order), if any is known."""
        found = [(-self.counts[self.position[w]], w) for w in words if w in self.position]
        return min(found)[1] if found else None

    def story_of(self, headline: int) -> int:
        """Its story; 0 for none (or a headline newer than the index)."""
        return self._story[headline] if headline < len(self._story) else 0

    def members(self, story: int) -> List[int]:
        """The story's headlines, newest first."""
        k = bisect.bisect_left(self._story_keys, story)
        if k == len(self._story_keys) or self._story_keys[k] != story:
            return []
        return list(self._story_members[self._story_starts[k]:self._story_starts[k + 1]])


_index: Optional[Index] = None
_started = False


def current() -> Optional[Index]:
    """The index this worker has mapped, or None before there is one."""
    return _index


def load() -> Index:
    """Maps the index file now and makes it the current one."""
    global _index
    _index = Index(index_path())
    return _index


def start() -> None:
    """Maps the index file in a background thread, and again whenever the refresher has written a new one."""
    global _started
    if _started or not settings.ARCHIVE_PATH:
        return
    _started = True

    def keep_loaded():
        loaded = None
        while True:
            try:
                st = os.stat(index_path())
                if (st.st_ino, st.st_mtime_ns) != loaded:
                    loaded = (st.st_ino, st.st_mtime_ns)  # Tried once: a broken file waits for the next one
                    index = load()
                    logger.info("Archive search index loaded: %d headlines, %d words", index.headlines, len(index.words))
            except FileNotFoundError:
                pass  # Not written yet: searches use SQLite
            except Exception as e:  # Searches keep the index they had, or SQLite
                logger.warning("Archive search index not loaded: %s", e)
            time.sleep(CHECK_SECONDS)

    threading.Thread(target=keep_loaded, name="archive-index", daemon=True).start()
