"""
The topic classifier: logistic regressions on headline embeddings, trained on
the stored labelled headlines and scored on the hand-labelled evaluation set.
"""

import time
from dataclasses import dataclass
from typing import List

import numpy as np
from sklearn.linear_model import LogisticRegression

TOPICS = ["tech", "science", "nature", "culture", "sport", "uplifting"]
OTHER = "other"  # Label for general news: politics, crime, economy...

# Decision thresholds. Live news is mostly hard news, so even a small false-positive
# rate floods a topic view: topics favour precision (0.9 instead of the best-F1 0.5).
TOPIC_THRESHOLD, UPLIFTING_THRESHOLD = 0.9, 0.8


@dataclass
class Classifier:
    version: str
    topic_model: LogisticRegression  # Classes: the topics and "other"
    uplifting_model: LogisticRegression  # Uplifting or not: a tone, so separate from the topic

    def predict(self, X: np.ndarray) -> List[List[str]]:
        classes = list(self.topic_model.classes_)
        topic_probs = self.topic_model.predict_proba(X)
        uplifting_probs = self.uplifting_model.predict_proba(X)[:, 1]
        tags = []
        for probs, uplifting in zip(topic_probs, uplifting_probs):
            row = [c for c, p in zip(classes, probs) if p >= TOPIC_THRESHOLD and c not in (OTHER, "uplifting")]
            if uplifting >= UPLIFTING_THRESHOLD:
                row.append("uplifting")
            tags.append(row)
        return tags


def label_scores(classifier: Classifier, X: np.ndarray, labels: List[str]) -> List[float]:
    """The topic model's probability of each row's label (one of its classes)."""
    classes = list(classifier.topic_model.classes_)
    probs = classifier.topic_model.predict_proba(X)
    return [float(row[classes.index(label)]) for row, label in zip(probs, labels)]


def train(X: np.ndarray, labels: List[str]) -> Classifier:
    def fit(y):
        return LogisticRegression(max_iter=2000, C=4, class_weight="balanced").fit(X, y)

    version = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + f"-n{len(labels)}"
    return Classifier(version, fit(labels), fit([label == "uplifting" for label in labels]))


def evaluate(classifier: Classifier, X: np.ndarray, gold: List[List[str]]) -> dict:
    """Macro F1 over the topics, and the share of untagged (hard news) items that got any tag."""
    predicted = [set(tags) for tags in classifier.predict(X)]
    gold = [set(tags) for tags in gold]
    f1s = []
    for topic in TOPICS:
        tp = sum(topic in p and topic in g for p, g in zip(predicted, gold))
        fp = sum(topic in p and topic not in g for p, g in zip(predicted, gold))
        fn = sum(topic not in p and topic in g for p, g in zip(predicted, gold))
        f1s.append(2 * tp / (2 * tp + fp + fn) if tp else 0.0)
    untagged = [p for p, g in zip(predicted, gold) if not g]
    return {
        "macro_f1": round(sum(f1s) / len(f1s), 4),
        "hard_news_leak": round(sum(bool(p) for p in untagged) / len(untagged), 4) if untagged else 0.0,
    }
