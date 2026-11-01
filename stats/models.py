"""
The worker's two models, each a line on the title's embedding, kept as JSON
(model/, written by train.py): how stressful a headline is, and whether it is
news the map should count at all.
"""

import json
from pathlib import Path

import numpy as np


class LinearModel:
    def __init__(self, weights: np.ndarray, intercept: float, threshold: float):
        self.weights, self.intercept, self.threshold = weights, intercept, threshold

    @classmethod
    def load(cls, path: Path) -> "LinearModel":
        data = json.loads(path.read_text())
        return cls(np.array(data["weights"], dtype=np.float32), data["intercept"], data["threshold"])

    def score(self, vectors: np.ndarray) -> np.ndarray:
        return vectors @ self.weights + self.intercept


def is_relevant(model: LinearModel, vectors: np.ndarray) -> np.ndarray:
    return model.score(vectors) >= model.threshold
