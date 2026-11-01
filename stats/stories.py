"""
The last days' stories, by their first headline's embedding: a new headline
joins the closest one it's close enough to, else starts its own.
"""

from typing import List, Optional

import numpy as np

STORY_THRESHOLD = 0.92  # The tagger's (ARCHITECTURE.md §6)
STORY_WINDOW_HOURS = 72
DIMENSIONS = 384


class StoryIndex:
    def __init__(self) -> None:
        self.ids: List[int] = []
        self.first_seen = np.zeros(1024)
        self.vectors = np.zeros((1024, DIMENSIONS), dtype=np.float32)

    def match(self, vector: np.ndarray, now: float) -> Optional[int]:
        """The closest story, if close enough and recent enough."""
        if not self.ids:
            return None
        count = len(self.ids)
        similarity = self.vectors[:count] @ vector
        similarity[self.first_seen[:count] < now - STORY_WINDOW_HOURS * 3600] = -1
        best = int(similarity.argmax())
        return self.ids[best] if similarity[best] >= STORY_THRESHOLD else None

    def insert(self, story_id: int, vector: np.ndarray, first_seen: float) -> None:
        if len(self.ids) == len(self.vectors):
            self.vectors = np.concatenate([self.vectors, np.zeros_like(self.vectors)])
            self.first_seen = np.concatenate([self.first_seen, np.zeros_like(self.first_seen)])
        self.vectors[len(self.ids)] = vector
        self.first_seen[len(self.ids)] = first_seen
        self.ids.append(story_id)

    def expire(self, now: float) -> None:
        keep = np.flatnonzero(self.first_seen[: len(self.ids)] >= now - STORY_WINDOW_HOURS * 3600)
        self.vectors[: len(keep)] = self.vectors[keep]
        self.first_seen[: len(keep)] = self.first_seen[keep]
        self.ids = [self.ids[i] for i in keep]
