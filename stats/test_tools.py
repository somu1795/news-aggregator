"""Tests for the tools run by hand: labelling and training."""

import base64
import json

import numpy as np

import backfill
import label
import train


def test_a_reply_becomes_a_label():
    assert label.parse_reply('{"stress": 2, "countries": ["ke"]}') == {"stress": 2, "countries": ["KE"]}


def test_only_two_countries_are_kept():
    assert len(label.parse_reply('{"stress": 1, "countries": ["KE", "UG", "TZ"]}')["countries"]) == 2


def test_a_bad_reply_is_no_label():
    assert label.parse_reply("not json") is None


def test_a_stress_off_the_scale_is_no_label():
    assert label.parse_reply('{"stress": 9, "countries": []}') is None


def test_a_slow_reply_means_the_gpu_is_busy():
    assert label.gpu_busy(1.0, [0.2] * 30) is True


def test_a_normal_reply_does_not():
    assert label.gpu_busy(0.3, [0.2] * 30) is False


def test_too_few_replies_decide_nothing():
    assert label.gpu_busy(5.0, [0.2] * 5) is False


def test_training_reads_the_export(tmp_path):
    vector = np.ones(384, dtype=np.float16)
    row = {"title": "T", "tags": "[]", "label": None, "story_embedding": base64.b64encode(vector.tobytes()).decode()}
    path = tmp_path / "export.jsonl"
    path.write_text(json.dumps(row) + "\n")
    rows, vectors = train.load(path)
    assert rows[0]["title"] == "T" and vectors.shape == (1, 384) and vectors.dtype == np.float32


def test_training_skips_rows_without_an_embedding(tmp_path):
    vector = base64.b64encode(np.ones(384, dtype=np.float16).tobytes()).decode()
    rows = [{"title": "T", "tags": "[]", "label": None, "story_embedding": vector},
            {"title": "U", "tags": "[]", "label": None, "story_embedding": None}]
    path = tmp_path / "export.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    rows, vectors = train.load(path)
    assert [row["title"] for row in rows] == ["T"] and len(vectors) == 1


def test_backfill_reads_a_missing_embedding_as_none():
    assert backfill.vector(None) is None
