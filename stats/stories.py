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
        self.first_seen: List[float] = []
        self.vectors = np.zeros((1024, DIMENSIONS), dtype=np.float32)

    def match(self, vector: np.ndarray, now: float) -> Optional[int]:
        """The closest story, if close enough and recent enough."""
        if not self.ids:
            return None
        similarity = self.vectors[: len(self.ids)] @ vector
        similarity[np.array(self.first_seen) < now - STORY_WINDOW_HOURS * 3600] = -1
        best = int(similarity.argmax())
        return self.ids[best] if similarity[best] >= STORY_THRESHOLD else None

    def insert(self, story_id: int, vector: np.ndarray, first_seen: float) -> None:
        if len(self.ids) == len(self.vectors):
            self.vectors = np.concatenate([self.vectors, np.zeros_like(self.vectors)])
        self.vectors[len(self.ids)] = vector
        self.ids.append(story_id)
        self.first_seen.append(first_seen)

    def expire(self, now: float) -> None:
        keep = [i for i, seen in enumerate(self.first_seen) if now - seen <= STORY_WINDOW_HOURS * 3600]
        self.vectors[: len(keep)] = self.vectors[keep]
        self.ids = [self.ids[i] for i in keep]
        self.first_seen = [self.first_seen[i] for i in keep]
