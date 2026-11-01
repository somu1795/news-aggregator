"""Tests for the embedder and the two linear models."""

import json
from types import SimpleNamespace

import numpy as np
import pytest

import embedder
from embedder import Embedder
from models import LinearModel, is_relevant


def model_file(tmp_path, weights, intercept=0.0, threshold=0.5):
    path = tmp_path / "model.json"
    path.write_text(json.dumps({"weights": weights, "intercept": intercept, "threshold": threshold}))
    return path


def test_score_is_a_dot_product_plus_intercept(tmp_path):
    model = LinearModel.load(model_file(tmp_path, [1.0, 2.0], intercept=0.5))
    assert model.score(np.array([[3.0, 4.0]], dtype=np.float32)).tolist() == [11.5]


def test_empty_batch_gives_empty_scores(tmp_path):
    model = LinearModel.load(model_file(tmp_path, [1.0, 2.0]))
    assert model.score(np.zeros((0, 2), dtype=np.float32)).shape == (0,)


def test_relevance_uses_the_models_threshold(tmp_path):
    model = LinearModel.load(model_file(tmp_path, [1.0], threshold=0.7))
    assert is_relevant(model, np.array([[0.6], [0.8]], dtype=np.float32)).tolist() == [False, True]


class FakeTokenizer:
    """Each word is one token; word "pad" counts as padding only through the mask."""

    def __init__(self):
        self.seen = []

    def enable_truncation(self, length):
        self.truncation = length

    def enable_padding(self):
        pass

    def encode_batch(self, texts):
        self.seen.extend(texts)
        longest = max(len(t.split()) for t in texts)
        return [SimpleNamespace(
            ids=[len(w) for w in t.split()] + [0] * (longest - len(t.split())),
            attention_mask=[1] * len(t.split()) + [0] * (longest - len(t.split())),
        ) for t in texts]


class FakeSession:
    """Hidden states of 2 dimensions: [token id, 1]; padding tokens get a large value that pooling must ignore."""

    def __init__(self, path, providers=None, sess_options=None):
        pass

    def get_inputs(self):
        return [SimpleNamespace(name="input_ids"), SimpleNamespace(name="attention_mask"), SimpleNamespace(name="token_type_ids")]

    def run(self, _, feeds):
        assert (feeds["token_type_ids"] == 0).all()
        ids, mask = feeds["input_ids"].astype(np.float32), feeds["attention_mask"]
        return [np.stack([ids + (mask == 0) * 1000, np.ones_like(ids)], axis=-1)]


@pytest.fixture
def fake_embedder(monkeypatch, tmp_path):
    tokenizer = FakeTokenizer()
    monkeypatch.setattr(embedder.Tokenizer, "from_file", lambda path: tokenizer)
    monkeypatch.setattr(embedder.ort, "InferenceSession", FakeSession)
    return Embedder(tmp_path)


def test_embeddings_are_normalized(fake_embedder):
    assert np.linalg.norm(fake_embedder.embed(["some title"])[0]) == pytest.approx(1.0)


def test_mean_pooling_ignores_padding(fake_embedder):
    alone = fake_embedder.embed(["short"])
    together = fake_embedder.embed(["short", "a much longer title here"])
    assert together[0] == pytest.approx(alone[0], abs=1e-6)


def test_query_prefix_is_used(fake_embedder):
    fake_embedder.embed(["A title"])
    assert fake_embedder.tokenizer.seen == ["query: A title"]


def test_no_titles_give_an_empty_array(fake_embedder):
    assert fake_embedder.embed([]).shape == (0, 384)


def test_titles_are_truncated(fake_embedder):
    assert fake_embedder.tokenizer.truncation == 48


def test_telemetry_is_off(fake_embedder, monkeypatch):
    calls = []
    monkeypatch.setattr(embedder.ort, "disable_telemetry_events", lambda: calls.append(True))
    Embedder.__init__(fake_embedder, fake_embedder.model_dir)
    assert calls == [True]
