"""
Story grouping: which headlines are about the same story (POST /group,
POST /stories), the pairs it was unsure of (GET /story-candidates), a judge's
verdicts on them (POST /story-verdicts) and the stories they changed
(GET /story-moves), on multilingual-e5-small embeddings.

Stories last: a headline gets its story once, when first grouped, and keeps it
(across views, refreshes and days) unless a judge's verdict merges two stories
or moves it out of one. A story's id is its first headline's rowid.
"""

import logging
import os
import time
from typing import TYPE_CHECKING, List

import numpy as np

from store import clean_title, to_blob

if TYPE_CHECKING:  # The API's request models, in tagger.py
    from tagger import Item, Verdict

logger = logging.getLogger("tagger")

# Cosine similarity at which two headlines are the same story. Chosen on 323 hand-labelled
# pairs of live headlines: it catches 80% of same-story pairs, and the only wrong merges
# were templated series (horoscopes, puzzle answers) and one pair of comparison articles.
STORY_THRESHOLD = float(os.environ.get("STORY_THRESHOLD", "0.92"))
# ...and the least it must share with the group's leader, so a group can't drift into a chain of
# ever-less-related stories. Checked on the live headlines of Oct 15, 2026: joining on any member
# at 0.92 with this floor added 18 same-story pairs and no wrong ones (e.g. one study reported as
# "Large dogs age more quickly…", "Why do small dogs live longer…", "The dog breeds that live longer…").
STORY_LEADER_THRESHOLD = float(os.environ.get("STORY_LEADER_THRESHOLD", "0.88"))
# Pairs from here up that end in different groups are "unsure": kept as candidates for a judge
# (an LLM), whose verdict then overrides the similarity. Below it, headlines are different stories.
STORY_CANDIDATE_THRESHOLD = float(os.environ.get("STORY_CANDIDATE_THRESHOLD", "0.84"))
STORY_CANDIDATES_MAX = 2000  # Per grouping: the most important unsure pairs
CANDIDATE_TTL_SECONDS = 86400  # Not seen in a grouping for this long: no longer on the page, dropped
VERDICT_TTL_SECONDS = 30 * 86400  # Headlines don't come back after a month
# A headline joins a story through any of its headlines first seen up to this long before it...
STORY_WINDOW_SECONDS = 3 * 86400
# ...if the story's first headline is at most this old: an older story is closed, so a daily series
# (horoscopes, puzzle answers) or a long-running subject starts a new story every few days. Chosen on
# all 115,566 grouped headlines of Oct 12-31, 2026, assigned in order of arrival: one study reported
# from Oct 28 to 31 (16 headlines, "Comets probably didn't seed life on Earth…") stays one story of 9
# (the judge's verdicts join the rest), and the largest stories are real ones (a train crash: 72
# headlines over 2 days) or such series, at most 4 days long. Joining two stories whenever a headline
# fits both chained different news together (a train crash and a train strike), so it doesn't.
STORY_MAX_AGE_SECONDS = 4 * 86400
ASSIGN_CHUNK = 500  # Headlines given stories at a time


def _unit(blobs) -> np.ndarray:
    """Stored embeddings as rows of length 1."""
    if not blobs:
        return np.zeros((0, 384), dtype=np.float32)
    X = np.stack([np.frombuffer(b, dtype=np.float16) for b in blobs]).astype(np.float32)
    return X / np.maximum(np.linalg.norm(X, axis=1, keepdims=True), 1e-9)


class StoryGrouping:
    """
    Tagger's story methods. Expects self.db (the database), self.story_embedder and
    self._store (stores headlines not stored yet), as Tagger has.
    """

    def group(self, items: List["Item"]) -> List[int]:
        """
        Each item's story (see stories). Unsure pairs are stored for the judge
        (story_candidates, see _store_candidates), most important first: items are given
        in the caller's ranking.
        """
        titles = [clean_title(item.title, item.source) for item in items]
        unique = list(dict.fromkeys(titles))
        vectors = self._story_vectors(unique, items)
        self._assign(unique)
        story_of = self._stories_of(unique)
        X = np.stack([vectors[t] for t in unique]) if unique else np.zeros((0, 384), dtype=np.float32)
        X /= np.maximum(np.linalg.norm(X, axis=1, keepdims=True), 1e-9)
        try:  # Only feeds the judge: never costs the grouping
            self._store_candidates(unique, X, np.array([story_of[t] for t in unique], dtype=np.int64))
        except Exception as e:
            logger.warning("Story candidates not stored: %s", e)
        return [story_of[title] for title in titles]

    def stories(self, items: List["Item"]) -> List[int]:
        """
        Each item's story; a headline without one yet gets one, in the order given: the
        story of its most similar headline first seen up to STORY_WINDOW_SECONDS before it,
        at or above STORY_THRESHOLD, if it is also within STORY_LEADER_THRESHOLD of that
        story's first headline and that one is at most STORY_MAX_AGE_SECONDS old; or a new
        story of its own. Any member catches a story worded several ways; the first
        headline's floor keeps a story from drifting into a chain of ever-less-related
        ones. A judge's verdict on a pair (story_verdicts) counts as the most (same story)
        or least (not) similar.
        """
        titles = [clean_title(item.title, item.source) for item in items]
        unique = list(dict.fromkeys(titles))
        self._story_vectors(unique, items)
        self._assign(unique)
        story_of = self._stories_of(unique)
        return [story_of[title] for title in titles]

    def assign_all(self) -> int:
        """Gives every grouped headline without a story one, in order of arrival (a database from before stories)."""
        titles = [t for (t,) in self.db.execute(
            "SELECT title FROM headlines WHERE story IS NULL AND story_embedding IS NOT NULL ORDER BY first_seen, rowid")]
        self._assign(titles)
        return len(titles)

    def _story_vectors(self, unique: List[str], items: List["Item"]) -> dict:
        """{title: story embedding}, embedding and storing those not stored yet."""
        vectors = {}
        for start in range(0, len(unique), 500):
            chunk = unique[start:start + 500]
            for title, blob in self.db.execute(
                f"SELECT title, story_embedding FROM headlines WHERE title IN ({','.join('?' * len(chunk))}) "
                "AND story_embedding IS NOT NULL", chunk,
            ):
                vectors[title] = np.frombuffer(blob, dtype=np.float16).astype(np.float32)
        missing = [title for title in unique if title not in vectors]
        if missing:
            self._store([(clean_title(item.title, item.source), item.source, item.link, None) for item in items])
            for title, v in zip(missing, self.story_embedder.embed(missing)):
                vectors[title] = v
            self.db.executemany("UPDATE headlines SET story_embedding = ? WHERE title = ?",
                                [(to_blob(vectors[t]), t) for t in missing])
            self.db.commit()
        return vectors

    def _stories_of(self, titles: List[str]) -> dict:
        story_of = {}
        for start in range(0, len(titles), 500):
            chunk = titles[start:start + 500]
            story_of.update(self.db.execute(
                f"SELECT title, story FROM headlines WHERE title IN ({','.join('?' * len(chunk))})", chunk))
        return story_of

    def _assign(self, titles: List[str]) -> None:
        """Gives these headlines without a story one, in the order given (see stories)."""
        rows = {}
        for start in range(0, len(titles), 500):
            chunk = titles[start:start + 500]
            for title, *rest in self.db.execute(
                f"SELECT title, rowid, first_seen, story_embedding FROM headlines WHERE title IN ({','.join('?' * len(chunk))}) "
                "AND story IS NULL AND story_embedding IS NOT NULL", chunk,
            ):
                rows[title] = rest
        new = [(title, *rows[title]) for title in titles if title in rows]
        for start in range(0, len(new), ASSIGN_CHUNK):
            self._assign_chunk(new[start:start + ASSIGN_CHUNK])
        self.db.commit()

    def _members(self, since: float):
        """The headlines in stories first seen since then: (titles, stories, first seen, unit story embeddings)."""
        rows = self.db.execute(
            "SELECT title, story, first_seen, story_embedding FROM headlines WHERE first_seen >= ? AND story IS NOT NULL",
            (since,)).fetchall()
        return ([r[0] for r in rows], np.array([r[1] for r in rows], dtype=np.int64), np.array([r[2] for r in rows]),
                _unit([r[3] for r in rows]))

    def _assign_chunk(self, chunk: list) -> None:
        """chunk: [(title, rowid, first_seen, story embedding)], each without a story."""
        titles = [row[0] for row in chunk]
        seen = np.array([row[2] for row in chunk])
        N = _unit([row[3] for row in chunk])
        member_titles, member_story, member_seen, M = self._members(float(seen.min()) - STORY_WINDOW_SECONDS)
        member_index = {title: i for i, title in enumerate(member_titles)}
        sims_members = N @ M.T
        sims_chunk = N @ N.T
        leaders = {}  # Story -> (title, first seen, unit vector) of its first headline
        stories = list(dict.fromkeys(member_story.tolist()))
        for start in range(0, len(stories), 500):
            part = stories[start:start + 500]
            for rowid, title, first_seen, blob in self.db.execute(
                f"SELECT rowid, title, first_seen, story_embedding FROM headlines WHERE rowid IN ({','.join('?' * len(part))})", part,
            ):
                leaders[rowid] = (title, first_seen, _unit([blob])[0])
        judged = self._judged(titles)
        story = np.zeros(len(chunk), dtype=np.int64)
        chunk_index = {title: k for k, title in enumerate(titles)}
        for k, (title, rowid, first_seen, _) in enumerate(chunk):
            # Every earlier headline: those already in stories, then this chunk's before it
            sims = np.concatenate([sims_members[k], sims_chunk[k, :k]])
            for other, same in judged.get(title, {}).items():
                i = member_index.get(other, len(member_titles) + chunk_index.get(other, k))
                if i < len(member_titles) + k:
                    sims[i] = 1.0 if same else -1.0
            sims[np.concatenate([member_seen, seen[:k]]) < first_seen - STORY_WINDOW_SECONDS] = -1.0
            their_story = np.concatenate([member_story, story[:k]])
            verdicts = judged.get(title, {})
            fits = {}  # Story -> whether its first headline allows this one
            story[k] = rowid  # A story of its own, unless one fits
            close = np.nonzero(sims >= STORY_THRESHOLD)[0]
            for i in close[np.argsort(-sims[close])]:
                s = int(their_story[i])
                if s not in fits:
                    lead_title, lead_seen, lead_vector = leaders[s]
                    verdict = verdicts.get(lead_title)
                    fits[s] = lead_seen >= first_seen - STORY_MAX_AGE_SECONDS and (
                        verdict if verdict is not None else float(lead_vector @ N[k]) >= STORY_LEADER_THRESHOLD)
                if fits[s]:
                    story[k] = s
                    break
            if story[k] == rowid:
                leaders[rowid] = (title, first_seen, N[k])
        self.db.executemany("UPDATE headlines SET story = ? WHERE rowid = ?",
                            [(int(s), row[1]) for s, row in zip(story, chunk)])

    def _judged(self, titles: List[str]) -> dict:
        """{title: {other title: same story}} for every verdict on these headlines."""
        judged: dict = {}
        for start in range(0, len(titles), 500):
            chunk = titles[start:start + 500]
            marks = ",".join("?" * len(chunk))
            for a, b, same in self.db.execute(
                f"SELECT a, b, same FROM story_verdicts WHERE a IN ({marks}) UNION SELECT a, b, same FROM story_verdicts WHERE b IN ({marks})",
                chunk + chunk,
            ):
                judged.setdefault(a, {})[b] = bool(same)
                judged.setdefault(b, {})[a] = bool(same)
        return judged

    def _store_candidates(self, titles: List[str], X: np.ndarray, story_of_row: np.ndarray) -> None:
        """
        Stores the unsure pairs: each of these headlines (in the caller's ranking: its position is
        the pair's rank) with the closest headline of each other story it has one from
        STORY_CANDIDATE_THRESHOLD up with among these, or from STORY_THRESHOLD up among the others
        first seen in the last STORY_WINDOW_SECONDS, unless judged already. So a story split when
        its headlines first came (in a different order) can be joined even after one side has
        left the views; at the lower floor, every headline is that close to ~35 recent stories.
        Drops candidates and verdicts no longer needed.
        """
        now = time.time()
        member_titles, member_story, _, M = self._members(now - STORY_WINDOW_SECONDS)
        known = set(member_titles)
        extra = [i for i, title in enumerate(titles) if title not in known]  # In the views, first seen before the window
        member_titles = member_titles + [titles[i] for i in extra]
        member_story = np.concatenate([member_story, story_of_row[extra]])
        M = np.concatenate([M, X[extra]])
        in_call = set(titles)
        floor = np.array([STORY_CANDIDATE_THRESHOLD if t in in_call else STORY_THRESHOLD for t in member_titles])
        judged = self._judged(titles)
        pairs: dict = {}
        for start in range(0, len(titles), 256):
            sims = X[start:start + 256] @ M.T
            rows, cols = np.nonzero((sims >= floor[None, :]) & (member_story[None, :] != story_of_row[start:start + 256, None]))
            closest: dict = {}  # (row, other story) -> (similarity, column)
            for r, c in zip(rows.tolist(), cols.tolist()):
                key = (r, int(member_story[c]))
                if key not in closest or sims[r, c] > closest[key][0]:
                    closest[key] = (float(sims[r, c]), c)
            for (r, _), (sim, c) in sorted(closest.items()):
                title, other = titles[start + r], member_titles[c]
                a, b = sorted((title, other))
                if other not in judged.get(title, {}) and (a, b) not in pairs and len(pairs) < STORY_CANDIDATES_MAX:
                    pairs[a, b] = (a, b, sim, start + r, now)
        self.db.executemany(
            "INSERT INTO story_candidates (a, b, similarity, rank, seen) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (a, b) DO UPDATE SET similarity = excluded.similarity, rank = excluded.rank, seen = excluded.seen",
            list(pairs.values()))
        self.db.execute("DELETE FROM story_candidates WHERE seen < ?", (now - CANDIDATE_TTL_SECONDS,))
        self.db.execute("DELETE FROM story_verdicts WHERE decided < ?", (now - VERDICT_TTL_SECONDS,))
        self.db.commit()

    def story_candidates(self, limit: int) -> List[dict]:
        """Unsure pairs with no verdict yet, from the latest groupings, most important first."""
        rows = self.db.execute(
            "SELECT a, b, similarity FROM story_candidates c WHERE seen >= ? "
            "AND NOT EXISTS (SELECT 1 FROM story_verdicts v WHERE v.a = c.a AND v.b = c.b) "
            "ORDER BY rank, similarity DESC LIMIT ?", (time.time() - 3600, limit))
        return [{"a": a, "b": b, "similarity": round(sim, 4)} for a, b, sim in rows]

    def store_verdicts(self, verdicts: List["Verdict"]) -> int:
        """
        Stores the verdicts and applies them to the stories: "same" merges two stories
        (into the older), "not" moves one of the two out of their story into one of its own
        (the later, unless it's the story's first headline). Each change is logged
        (story_moves), for copies of the stories (the archive) to follow.
        """
        now = time.time()
        rows = [(*sorted((v.a, v.b)), int(v.same), v.model, now) for v in verdicts if v.a != v.b]
        self.db.executemany(
            "INSERT INTO story_verdicts (a, b, same, model, decided) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (a, b) DO UPDATE SET same = excluded.same, model = excluded.model, decided = excluded.decided",
            rows)
        self.db.executemany("DELETE FROM story_candidates WHERE a = ? AND b = ?", [r[:2] for r in rows])
        for a, b, same, _, _ in rows:
            found = dict((title, (rowid, story)) for title, rowid, story in self.db.execute(
                "SELECT title, rowid, story FROM headlines WHERE title IN (?, ?) AND story IS NOT NULL", (a, b)))
            if len(found) < 2:
                continue  # Not grouped yet: the verdict applies when they are
            (row_a, story_a), (row_b, story_b) = found[a], found[b]
            if same and story_a != story_b:
                old, new = max(story_a, story_b), min(story_a, story_b)
                self.db.execute("UPDATE headlines SET story = ? WHERE story = ?", (new, old))
                self.db.execute("INSERT INTO story_moves (old, new, title, moved) VALUES (?, ?, NULL, ?)", (old, new, now))
            elif not same and story_a == story_b:
                title, rowid = (b, row_b) if row_a == story_a or (row_b != story_b and row_a < row_b) else (a, row_a)
                self.db.execute("UPDATE headlines SET story = rowid WHERE rowid = ?", (rowid,))
                self.db.execute("INSERT INTO story_moves (old, new, title, moved) VALUES (?, ?, ?, ?)", (story_a, rowid, title, now))
        self.db.commit()
        return len(rows)

    def story_moves(self, after: int, limit: int) -> List[dict]:
        """The story changes logged after id `after`, oldest first: a whole story (title None) or one headline."""
        return [{"id": i, "old": old, "new": new, "title": title} for i, old, new, title in self.db.execute(
            "SELECT id, old, new, title FROM story_moves WHERE id > ? ORDER BY id LIMIT ?", (after, limit))]
