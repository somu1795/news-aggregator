"""
Tagger tests with a fake embedder (words hashed into a vector), so they run
without OpenVINO or an NPU: storage, tagging, labels, and the retraining gate.

Run in the tagger image, or with requirements.txt + pytest installed:
    python -m pytest -q test_tagger.py
"""

import json
import zlib

import numpy as np
import pytest

import tagger as tagger_module
from tagger import Item, Tagger, clean_title

SEED = (
    [{"title": f"team wins cup final match {i}", "label": "sport"} for i in range(10)]
    + [{"title": f"new phone chip software launch {i}", "label": "tech"} for i in range(10)]
    + [{"title": f"minister budget vote parliament {i}", "label": "other"} for i in range(10)]
    + [{"title": f"kind neighbours rescue lost puppy {i}", "label": "uplifting"} for i in range(10)]
)
EVAL = [
    {"title": "team wins league match", "tags": ["sport"]},
    {"title": "phone software update", "tags": ["tech"]},
    {"title": "parliament budget row", "tags": []},
]


class FakeEmbedder:
    device = "FAKE"

    def __init__(self):
        self.embedded = []

    def embed(self, titles):
        self.embedded += titles
        vectors = np.zeros((len(titles), 384), dtype=np.float32)
        for row, title in zip(vectors, titles):
            for word in title.lower().split():
                if not word.isdigit():
                    row[zlib.crc32(word.encode()) % 384] += 1
        return vectors / np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-9)


@pytest.fixture
def tagger(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "seed.json").write_text(json.dumps(SEED))
    (bundled / "eval.json").write_text(json.dumps(EVAL))
    monkeypatch.setattr(tagger_module, "BUNDLED", bundled)
    t = Tagger(FakeEmbedder(), tmp_path)
    t.start()
    return t


def test_clean_title():
    assert clean_title("Big  win - The Paper", "The Paper") == "Big win"
    assert clean_title("Big win - The Paper", "Other") == "Big win - The Paper"


def test_tags_and_stores_each_headline_once(tagger):
    items = [Item(title="Team wins cup final - Sports Daily", source="Sports Daily"), Item(title="New phone chip")]
    before = len(tagger.embedder.embedded)
    assert tagger.tag(items) == [["sport"], ["tech"]]
    assert tagger.tag(items) == [["sport"], ["tech"]]
    assert tagger.embedder.embedded[before:] == ["Team wins cup final", "New phone chip"]  # Embedded once, cleaned
    stored = dict(tagger.db.execute("SELECT title, source FROM headlines WHERE title = 'Team wins cup final'").fetchall())
    assert stored == {"Team wins cup final": "Sports Daily"}


def test_feed_label_is_stored_and_a_topic_beats_other(tagger):
    tagger.tag([Item(title="Fans celebrate", label="other")])
    tagger.tag([Item(title="Fans celebrate", label="sport")])
    tagger.tag([Item(title="Fans celebrate", label="other")])  # Doesn't downgrade
    assert tagger.db.execute("SELECT label FROM headlines WHERE title = 'Fans celebrate'").fetchone() == ("sport",)


def test_retrain_keeps_the_current_classifier_if_the_candidate_is_worse(tagger, monkeypatch):
    current = tagger.classifier
    monkeypatch.setattr(tagger_module, "evaluate",
                        lambda classifier, X, gold: {"macro_f1": 0.9 if classifier is current else 0.1})
    tagger.retrain()
    assert tagger.classifier is current


def test_retrain_replaces_and_retags(tagger, monkeypatch):
    tagger.tag([Item(title="Team wins cup final")])
    monkeypatch.setattr(tagger_module.time, "strftime", lambda *args: "20990101T000000Z")
    tagger.retrain()
    assert tagger.classifier.version.startswith("20990101T000000Z")
    assert tagger.tag([Item(title="Team wins cup final")]) == [["sport"]]
    model = tagger.db.execute("SELECT model FROM headlines WHERE title = 'Team wins cup final'").fetchone()[0]
    assert model == tagger.classifier.version
    # Saved, and loaded on the next start
    restarted = Tagger(FakeEmbedder(), tagger.model_path.parent)
    restarted.start()
    assert restarted.classifier.version == tagger.classifier.version


def test_eval_headlines_are_never_trained_on(tagger, monkeypatch):
    tagger.tag([Item(title=EVAL[0]["title"], label="sport")])
    trained = []
    monkeypatch.setattr(tagger_module, "train", lambda X, labels: trained.append(len(labels)) or tagger.classifier)
    tagger.retrain()
    assert trained == [len(SEED)]
