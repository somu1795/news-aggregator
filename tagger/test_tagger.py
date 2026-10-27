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

import classifier as classifier_module
import stories as stories_module
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
    monkeypatch.setattr(classifier_module, "TOPIC_THRESHOLD", 0.5)  # The toy embedding's classifier is never very confident
    t = Tagger(FakeEmbedder(), tmp_path, story_embedder=FakeEmbedder())
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
    monkeypatch.setattr(classifier_module.time, "strftime", lambda *args: "20990101T000000Z")
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


def test_retrain_uses_only_the_newest_labelled_headlines(tagger, monkeypatch):
    tagger.tag([Item(title="Brand new sports story", label="sport")])
    trained = []
    monkeypatch.setattr(tagger_module, "TRAIN_MAX_ROWS", 3)
    monkeypatch.setattr(tagger_module, "train", lambda X, labels: trained.append(labels) or tagger.classifier)
    tagger.retrain()
    assert len(trained[0]) == 3 and trained[0][0] == "sport"  # Newest first, capped


def test_group_puts_the_same_story_together(tagger, monkeypatch):
    monkeypatch.setattr(stories_module, "STORY_THRESHOLD", 0.8)
    items = [
        Item(title="apple unveils homepod mini two with siri", source="A"),
        Item(title="minister resigns over budget vote", source="B"),
        Item(title="apple unveils homepod mini two with siri today", source="C"),
        Item(title="apple unveils homepod mini two with siri - D", source="D"),  # Google's publisher suffix
        Item(title="minister resigns over budget vote", source="E"),  # Same title, another outlet
    ]
    groups = tagger.group(items)
    assert groups[0] == groups[2] == groups[3] and groups[1] == groups[4] and groups[0] != groups[1]
    # Story embeddings are stored: grouping again embeds nothing
    tagger.story_embedder.embedded.clear()
    assert tagger.group(items) == groups and tagger.story_embedder.embedded == []


def test_group_joins_a_story_worded_several_ways(tagger, monkeypatch):
    monkeypatch.setattr(stories_module, "STORY_THRESHOLD", 0.65)
    monkeypatch.setattr(stories_module, "STORY_LEADER_THRESHOLD", 0.5)
    a = "big dogs age faster study finds"
    b = "big dogs age faster scientists say"       # 0.67 to a
    c = "why big dogs age faster scientists say"   # 0.93 to b, 0.62 to a: joins through b
    d = "minister resigns over budget vote"
    groups = tagger.group([Item(title=a), Item(title=b), Item(title=c), Item(title=d)])
    assert groups[0] == groups[1] == groups[2] != groups[3]


def test_group_stays_close_to_its_leader_so_stories_dont_chain(tagger, monkeypatch):
    monkeypatch.setattr(stories_module, "STORY_THRESHOLD", 0.7)
    monkeypatch.setattr(stories_module, "STORY_LEADER_THRESHOLD", 0.5)
    a = "red blue green yellow orange purple"
    b = "red blue green yellow orange black white"   # Close to a
    c = "red blue green black white pink grey"       # Close to b, not to a
    groups = tagger.group([Item(title=a), Item(title=b), Item(title=c)])
    assert groups[0] == groups[1] and groups[2] != groups[0]


def test_unsure_pairs_are_kept_for_the_judge(tagger, monkeypatch):
    monkeypatch.setattr(stories_module, "STORY_THRESHOLD", 0.9)
    monkeypatch.setattr(stories_module, "STORY_LEADER_THRESHOLD", 0.9)
    monkeypatch.setattr(stories_module, "STORY_CANDIDATE_THRESHOLD", 0.6)
    a, b, c = "big dogs age faster study finds", "big dogs age faster scientists say", "minister resigns over budget vote"
    groups = tagger.group([Item(title=a), Item(title=b), Item(title=c)])
    assert groups[0] != groups[1]  # ~0.7: below the threshold, above the candidates'
    [pair] = tagger.story_candidates(10)  # Not c: unrelated
    assert (pair["a"], pair["b"]) == tuple(sorted((a, b))) and 0.6 <= pair["similarity"] < 0.9


def test_a_verdict_overrides_similarity_and_retires_the_candidate(tagger, monkeypatch):
    monkeypatch.setattr(stories_module, "STORY_THRESHOLD", 0.9)
    monkeypatch.setattr(stories_module, "STORY_LEADER_THRESHOLD", 0.9)
    monkeypatch.setattr(stories_module, "STORY_CANDIDATE_THRESHOLD", 0.6)
    a, b = "big dogs age faster study finds", "big dogs age faster scientists say"
    tagger.group([Item(title=a), Item(title=b)])
    assert tagger.store_verdicts([tagger_module.Verdict(a=b, b=a, same=True, model="m")]) == 1  # Either order
    assert tagger.story_candidates(10) == []
    groups = tagger.group([Item(title=a), Item(title=b)])
    assert groups[0] == groups[1]
    assert tagger.story_candidates(10) == []  # Judged pairs aren't asked again


def test_a_no_verdict_keeps_similar_headlines_apart(tagger, monkeypatch):
    monkeypatch.setattr(stories_module, "STORY_THRESHOLD", 0.7)  # 0.71 apart
    monkeypatch.setattr(stories_module, "STORY_LEADER_THRESHOLD", 0.7)
    a, b = "champions league live latest updates bodo dortmund", "champions league live latest updates villa fenerbahce"
    assert len(set(tagger.group([Item(title=a), Item(title=b)]))) == 1
    tagger.store_verdicts([tagger_module.Verdict(a=a, b=b, same=False, model="m")])
    assert len(set(tagger.group([Item(title=a), Item(title=b)]))) == 2


def test_a_restart_does_not_retrain_again_after_a_rejected_attempt(tagger, monkeypatch):
    """A rejected candidate leaves the saved classifier old; the next start must still wait a full interval."""
    import asyncio
    import os
    import time
    week_ago = time.time() - 7 * 86400
    os.utime(tagger.model_path, (week_ago, week_ago))  # Saved a week ago; every later candidate was rejected
    assert tagger_module.retrain_due(tagger) < time.time()  # Due now

    attempts = []

    async def one_cycle():
        def attempt():
            attempts.append(time.time())
            raise RuntimeError("candidate rejected or failed")  # Either way the saved classifier stays as it was
        monkeypatch.setattr(tagger, "retrain", attempt)
        loop = asyncio.create_task(tagger_module.retrain_periodically(tagger))
        while not attempts:
            await asyncio.sleep(0.01)
        loop.cancel()

    asyncio.run(one_cycle())
    assert len(attempts) == 1
    # A restart now (a new retrain_periodically) waits a full interval from that attempt
    assert tagger_module.retrain_due(tagger) >= attempts[0] + tagger_module.RETRAIN_INTERVAL_HOURS * 3600 - 1
