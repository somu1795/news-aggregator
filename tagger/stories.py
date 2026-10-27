"""
Story grouping: which headlines are about the same story (POST /group), the
pairs it was unsure of (GET /story-candidates) and a judge's verdicts on them
(POST /story-verdicts), on multilingual-e5-small embeddings.
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


class StoryGrouping:
    """
    Tagger's story methods. Expects self.db (the database), self.story_embedder and
    self._store (stores headlines not stored yet), as Tagger has.
    """

    def group(self, items: List["Item"]) -> List[int]:
        """
        A group number per item: items about the same story share one. Items are taken in
        the order given (the caller's ranking); each joins the group of the most similar
        earlier headline at or above STORY_THRESHOLD, if it is also within
        STORY_LEADER_THRESHOLD of that group's leader (its first headline), or leads a new
        group. Any member catches a story worded several ways; the leader's floor keeps a
        group from drifting into a chain of ever-less-related stories. A judge's verdict on
        a pair (story_verdicts) counts as the most (same story) or least (not) similar.
        Pairs left in different groups from STORY_CANDIDATE_THRESHOLD up, with no verdict,
        are stored for the judge (story_candidates).
        """
        titles = [clean_title(item.title, item.source) for item in items]
        unique = list(dict.fromkeys(titles))
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
        X = np.stack([vectors[t] for t in unique]) if unique else np.zeros((0, 384), dtype=np.float32)
        X /= np.maximum(np.linalg.norm(X, axis=1, keepdims=True), 1e-9)
        verdicts = self._verdicts(unique)
        position = {title: row for row, title in enumerate(unique)}
        judged: dict = {}  # Later row -> {earlier row: same story}
        for (a, b), same in verdicts.items():
            first, second = sorted((position[a], position[b]))
            judged.setdefault(second, {})[first] = same
        leaders: List[int] = []  # Row of each group's first headline
        group_of_row = np.zeros(len(unique), dtype=np.int64)
        for row in range(len(unique)):
            sims = X[:row] @ X[row]
            for earlier, same in judged.get(row, {}).items():
                sims[earlier] = 1.0 if same else -1.0
            close = sims >= STORY_THRESHOLD
            close &= sims[np.array(leaders, dtype=np.int64)[group_of_row[:row]]] >= STORY_LEADER_THRESHOLD
            if close.any():
                group_of_row[row] = group_of_row[int(np.argmax(np.where(close, sims, -1.0)))]
            else:
                group_of_row[row] = len(leaders)
                leaders.append(row)
        try:  # Only feeds the judge: never costs the grouping
            self._store_candidates(unique, X, group_of_row, verdicts)
        except Exception as e:
            logger.warning("Story candidates not stored: %s", e)
        group_of = dict(zip(unique, group_of_row.tolist()))
        return [group_of[title] for title in titles]

    def _verdicts(self, titles: List[str]) -> dict:
        """{(a, b): same story} for the judged pairs among these titles (a < b)."""
        wanted, verdicts = set(titles), {}
        for start in range(0, len(titles), 500):
            chunk = titles[start:start + 500]
            for a, b, same in self.db.execute(
                f"SELECT a, b, same FROM story_verdicts WHERE a IN ({','.join('?' * len(chunk))})", chunk,
            ):
                if b in wanted:
                    verdicts[a, b] = bool(same)
        return verdicts

    def _store_candidates(self, titles: List[str], X: np.ndarray, group_of_row: np.ndarray, verdicts: dict) -> None:
        """Stores the unsure pairs (see group); drops candidates and verdicts no longer needed."""
        rows_a, rows_b = np.triu_indices(len(titles), 1)
        sims = (X @ X.T)[rows_a, rows_b]
        unsure = (sims >= STORY_CANDIDATE_THRESHOLD) & (group_of_row[rows_a] != group_of_row[rows_b])
        order = np.argsort(rows_b[unsure], kind="stable")[:STORY_CANDIDATES_MAX]  # rows_b is the later one
        now = time.time()
        pairs = []
        for i, j, sim in zip(rows_a[unsure][order], rows_b[unsure][order], sims[unsure][order]):
            a, b = sorted((titles[i], titles[j]))
            if (a, b) not in verdicts:
                pairs.append((a, b, float(sim), int(j), now))
        self.db.executemany(
            "INSERT INTO story_candidates (a, b, similarity, rank, seen) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (a, b) DO UPDATE SET similarity = excluded.similarity, rank = excluded.rank, seen = excluded.seen",
            pairs)
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
        now = time.time()
        rows = [(*sorted((v.a, v.b)), int(v.same), v.model, now) for v in verdicts if v.a != v.b]
        self.db.executemany(
            "INSERT INTO story_verdicts (a, b, same, model, decided) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (a, b) DO UPDATE SET same = excluded.same, model = excluded.model, decided = excluded.decided",
            rows)
        self.db.executemany("DELETE FROM story_candidates WHERE a = ? AND b = ?", [r[:2] for r in rows])
        self.db.commit()
        return len(rows)
