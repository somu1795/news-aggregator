"""
Tagger tests with a fake embedder (words hashed into a vector), so they run
without OpenVINO or an NPU: storage, tagging, labels, and the retraining gate.

Run in the tagger image, or with requirements.txt + pytest installed:
    python -m pytest -q test_tagger.py
"""

import json
import time
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


def test_retrain_uses_the_newest_labelled_headlines_and_always_the_seed(tagger, monkeypatch):
    tagger.tag([Item(title="Old sports story", label="sport")])
    tagger.db.execute("UPDATE headlines SET first_seen = 1 WHERE title = 'Old sports story'")  # Older than the seed
    tagger.db.commit()
    tagger.tag([Item(title="Brand new sports story", label="sport")])
    trained = []
    monkeypatch.setattr(tagger_module, "TRAIN_MAX_ROWS", 3)
    monkeypatch.setattr(tagger_module, "train", lambda X, labels: trained.append((X, labels)) or tagger.classifier)
    tagger.retrain()
    X, labels = trained[0]
    # The newest 3 (the brand new story and 2 seed headlines), then the rest of the seed; never the old story
    assert len(labels) == 1 + len(SEED) and labels[0] == "sport"
    assert X.shape == (len(labels), 384) and X.dtype == np.float32
    assert sorted(labels[1:]) == sorted(row["label"] for row in SEED)


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


def test_a_headline_is_paired_for_the_judge_with_recent_stories_no_longer_in_the_views(tagger, monkeypatch):
    monkeypatch.setattr(stories_module, "STORY_THRESHOLD", 0.7)
    monkeypatch.setattr(stories_module, "STORY_LEADER_THRESHOLD", 0.9)
    monkeypatch.setattr(stories_module, "STORY_CANDIDATE_THRESHOLD", 0.6)
    a, a2 = "big dogs age faster study finds", "big dogs age faster study finds today"  # One story (0.94)
    b = "big dogs age faster scientists say"  # 0.72 to a, 0.68 to a2, too far from a to join: another story

    def candidates_when_grouping(*titles):
        tagger.db.execute("DELETE FROM story_candidates")
        tagger.group([Item(title=t) for t in titles])
        return [{p["a"], p["b"]} for p in tagger.story_candidates(10)]

    tagger.group([Item(title=a), Item(title=a2)])
    # a and a2 have left the views: paired with the closest of their story, as close as STORY_THRESHOLD
    assert candidates_when_grouping("minister resigns over budget vote", b) == [{a, b}]
    monkeypatch.setattr(stories_module, "STORY_THRESHOLD", 0.75)
    assert candidates_when_grouping(b) == []  # 0.72: enough within a grouping only
    assert candidates_when_grouping(a, b) == [{a, b}]
    monkeypatch.setattr(stories_module, "STORY_THRESHOLD", 0.7)
    seen_days_ago(tagger, [a, a2], 3.5)  # Beyond the window
    assert candidates_when_grouping(b) == []


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


def seen_days_ago(tagger, titles, days):
    tagger.db.executemany("UPDATE headlines SET first_seen = ? WHERE title = ?",
                          [(time.time() - days * 86400, t) for t in titles])


def test_a_story_lasts_from_call_to_call(tagger, monkeypatch):
    monkeypatch.setattr(stories_module, "STORY_THRESHOLD", 0.8)
    a, b = "apple unveils homepod mini two with siri", "apple unveils homepod mini two with siri today"
    [story] = tagger.group([Item(title=a)])
    assert tagger.group([Item(title="minister resigns over budget vote"), Item(title=b)])[1] == story
    assert tagger.stories([Item(title=b, source="X"), Item(title=a)]) == [story, story]


def test_a_headline_joins_only_recent_headlines_of_a_story_not_too_old(tagger, monkeypatch):
    monkeypatch.setattr(stories_module, "STORY_THRESHOLD", 0.8)
    a, b = "apple unveils homepod mini two with siri", "apple unveils homepod mini two with siri today"
    c, d = "apple unveils homepod mini two with siri tonight", "apple unveils homepod mini two with siri again"
    [story, _] = tagger.group([Item(title=a), Item(title=b)])
    seen_days_ago(tagger, [a, b], 3.5)  # Both further back than the window
    [later] = tagger.group([Item(title=c)])
    assert later != story
    seen_days_ago(tagger, [c], 4.5)  # The new story's first headline, too old for it to go on
    assert tagger.group([Item(title=d)])[0] not in (story, later)


def test_stories_are_given_in_order_of_arrival_to_a_database_from_before_them(tagger, monkeypatch):
    monkeypatch.setattr(stories_module, "STORY_THRESHOLD", 0.8)
    a, b = "apple unveils homepod mini two with siri", "apple unveils homepod mini two with siri today"
    c = "minister resigns over budget vote"
    tagger.group([Item(title=b), Item(title=a), Item(title=c)])
    seen_days_ago(tagger, [a], 1)
    tagger.db.execute("UPDATE headlines SET story = NULL")
    assert tagger.assign_all() == 3
    stories = tagger.stories([Item(title=a), Item(title=b), Item(title=c)])
    first = tagger.db.execute("SELECT rowid FROM headlines WHERE title = ?", (a,)).fetchone()[0]
    assert stories[0] == stories[1] == first != stories[2]  # Led by the first seen
    assert tagger.assign_all() == 0


def test_stories_stores_no_pairs_for_the_judge(tagger, monkeypatch):
    monkeypatch.setattr(stories_module, "STORY_THRESHOLD", 0.9)
    monkeypatch.setattr(stories_module, "STORY_CANDIDATE_THRESHOLD", 0.6)
    a, b = "big dogs age faster study finds", "big dogs age faster scientists say"
    assert len(set(tagger.stories([Item(title=a), Item(title=b)]))) == 2
    assert tagger.story_candidates(10) == []


def test_verdicts_merge_and_split_stories_and_log_it(tagger, monkeypatch):
    monkeypatch.setattr(stories_module, "STORY_THRESHOLD", 0.9)
    monkeypatch.setattr(stories_module, "STORY_LEADER_THRESHOLD", 0.9)
    a, b = "big dogs age faster study finds", "big dogs age faster scientists say"
    c = "big dogs age faster study finds today"  # Joins a's story
    story_a, story_b, _ = tagger.group([Item(title=a), Item(title=b), Item(title=c)])
    assert story_a != story_b
    tagger.store_verdicts([tagger_module.Verdict(a=b, b=c, same=True)])  # Not the first headlines: still merges
    assert tagger.stories([Item(title=a), Item(title=b), Item(title=c)]) == [story_a] * 3
    tagger.store_verdicts([tagger_module.Verdict(a=a, b=b, same=False)])
    rowid_b = tagger.db.execute("SELECT rowid FROM headlines WHERE title = ?", (b,)).fetchone()[0]
    assert tagger.stories([Item(title=a), Item(title=b), Item(title=c)]) == [story_a, rowid_b, story_a]
    moves = tagger.story_moves(0, 10)
    assert [(m["old"], m["new"], m["title"]) for m in moves] == [(story_b, story_a, None), (story_a, rowid_b, b)]
    assert tagger.story_moves(moves[0]["id"], 10) == moves[1:]


def test_a_verdict_on_headlines_not_grouped_yet_changes_no_story(tagger):
    tagger.store_verdicts([tagger_module.Verdict(a="one thing", b="another", same=True)])
    assert tagger.story_moves(0, 10) == []


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
