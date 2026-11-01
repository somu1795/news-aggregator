"""Tests for the worker: from stream entries to the published file."""

import json
import sqlite3
from pathlib import Path

import fakeredis
import numpy as np
import pytest

import worker
from models import LinearModel
from places import Places
from store import Store
from stories import StoryIndex

NOW = 1_800_000_000.0
DAY = 86400.0


def vector(seed):
    rng = np.random.default_rng(seed)
    v = rng.normal(size=384).astype(np.float32)
    return v / np.linalg.norm(v)


class FakeEmbedder:
    """The same words give the same vector; " again" is dropped first."""

    def embed(self, titles):
        out = np.stack([vector(abs(hash(t.replace(" again", ""))) % 2**32) for t in titles]) if titles else np.zeros((0, 384), np.float32)
        return out


class ConstantModel(LinearModel):
    def __init__(self, value):
        self.value, self.threshold = value, 0.5

    def score(self, vectors):
        return np.full(len(vectors), self.value, dtype=np.float32)


class WordRelevance(ConstantModel):
    """Set through state.irrelevant: how many of the first vectors of the next batch to reject."""

    def __init__(self):
        super().__init__(1.0)
        self.reject_first = 0

    def score(self, vectors):
        scores = np.ones(len(vectors), dtype=np.float32)
        scores[: self.reject_first] = 0
        return scores


@pytest.fixture
def state(tmp_path):
    places = Places({"Kenya": ["KE", None, None], "Iran": ["IR", None, None]}, {}, {"KE": [-1.3, 36.8]}, {"Yonhap": "KR"})
    return worker.State(store=Store(tmp_path / "stats.db"), places=places, embedder=FakeEmbedder(),
                        stress=ConstantModel(2.5), relevant=WordRelevance(), index=StoryIndex(),
                        public=tmp_path / "heatmap.json")


def fields(title="Kenya votes", link="https://example.com/1", source="Paper", label="", published=""):
    return {"title": title, "link": link, "source": source, "label": label, "published": published}


def entry(n=1, **kwargs):
    return (f"{int(NOW * 1000) + n}-0", fields(**kwargs))


def stories(state):
    return state.store.stories_since(0)


def test_parse_skips_missing_fields():
    assert worker.parse_entry(f"{int(NOW * 1000)}-0", {"title": "x"}) is None


def test_parse_drops_unsafe_links():
    assert worker.parse_entry(entry(link="javascript:alert(1)")[0], fields(link="javascript:alert(1)")) is None


def test_parse_skips_an_empty_title():
    assert worker.parse_entry(*entry(title="   ")) is None


def test_parse_decodes_the_title():
    assert worker.parse_entry(*entry(title="Zamb&#xe9;zia"))["title"] == "Zambézia"


def test_parse_uses_the_feed_date():
    published = NOW - 3600
    assert worker.parse_entry(*entry(published=str(published)))["when"] == published


def test_parse_tolerates_a_bad_date():
    assert worker.parse_entry(*entry(published="n/a"))["when"] == pytest.approx(NOW, abs=1)


def test_parse_ignores_a_future_date():
    assert worker.parse_entry(*entry(published=str(NOW + DAY)))["when"] == pytest.approx(NOW, abs=1)


def test_parse_marks_a_very_old_date():
    assert worker.parse_entry(*entry(published=str(NOW - 30 * DAY)))["stale"] is True


def test_parse_keeps_an_undated_item_fresh():
    assert worker.parse_entry(*entry(published=""))["stale"] is False


def test_an_old_item_makes_no_story(state):
    worker.process([entry(published=str(NOW - 30 * DAY))], state)
    assert stories(state) == [] and state.store.headline_count() == 1


def test_an_old_item_is_not_embedded(state):
    state.embedder = None  # Would fail if used
    worker.process([entry(published=str(NOW - 30 * DAY))], state)


def test_parse_removes_googles_publisher_suffix():
    item = worker.parse_entry(*entry(title="Budget vote delayed - The Times of India", source="The Times of India"))
    assert item["title"] == "Budget vote delayed"


def test_parse_keeps_a_dash_that_is_not_the_publisher():
    assert worker.parse_entry(*entry(title="Kenya - a nation votes", source="Paper"))["title"] == "Kenya - a nation votes"


def test_an_outlet_name_is_not_a_place_in_the_title(state):
    state.places = Places({"India": ["IN", None, None]}, {}, {}, {})
    worker.process([entry(title="Budget vote delayed - The Times of India", source="The Times of India")], state)
    assert stories(state) == []


def test_a_new_headline_starts_a_story(state):
    worker.process([entry()], state)
    assert [s.title for s in stories(state)] == ["Kenya votes"]


def test_duplicate_links_are_ignored(state):
    worker.process([entry(1), entry(2)], state)
    worker.process([entry(3)], state)
    assert stories(state)[0].outlets == {"Paper"} and state.store.headline_count() == 1


@pytest.mark.parametrize("label", ["sport", "culture", "tech", "science", "nature"])
def test_labelled_topic_feeds_are_dropped(state, label):
    worker.process([entry(label=label)], state)
    assert stories(state) == []


@pytest.mark.parametrize("label", ["", "other", "uplifting"])
def test_other_labels_are_kept(state, label):
    worker.process([entry(label=label)], state)
    assert len(stories(state)) == 1


def test_irrelevant_headlines_are_dropped(state):
    state.relevant.reject_first = 1
    worker.process([entry()], state)
    assert stories(state) == []


def test_dropped_headlines_are_not_embedded_twice(state):
    state.relevant.reject_first = 1
    worker.process([entry()], state)
    state.embedder = None  # Would fail if used
    worker.process([entry(2)], state)


def test_unplaced_headlines_make_no_story(state):
    worker.process([entry(title="Nowhere in particular")], state)
    assert stories(state) == []


def test_stress_is_kept_on_the_scale(state):
    state.stress = ConstantModel(9.0)
    worker.process([entry()], state)
    assert stories(state)[0].stress == 3.0


def test_same_story_merges_outlets(state):
    worker.process([entry(1), entry(2, title="Kenya votes again", link="https://example.com/2", source="Other")], state)
    assert stories(state)[0].outlets == {"Paper", "Other"} and len(stories(state)) == 1


def test_a_story_averages_its_stress(state):
    worker.process([entry(1)], state)
    state.stress = ConstantModel(1.5)
    worker.process([entry(2, title="Kenya votes again", link="https://example.com/2")], state)
    assert stories(state)[0].stress == 2.0



def test_a_title_outranks_an_outlet_guess(state):
    worker.process([entry(1, title="Iran talks", source="Yonhap")], state)
    assert stories(state)[0].countries == {"IR": True}


def test_a_bad_entry_does_not_stop_the_batch(state):
    worker.process([("1-0", {"title": "x"}), entry()], state)
    assert len(stories(state)) == 1


def test_a_position_is_saved_with_the_batch(state):
    worker.process([entry(5)], state)
    assert state.store.last_id() == entry(5)[0]


def test_a_crash_midway_keeps_nothing(state, monkeypatch):
    monkeypatch.setattr(state.index, "insert", lambda *a: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError):
        worker.process([entry()], state)
    assert state.store.last_id() == "0" and state.store.headline_count() == 0


def redis_with(*entries):
    client = fakeredis.FakeRedis(decode_responses=True)
    for entry_id, data in entries:
        client.xadd(worker.STREAM, data, id=entry_id)
    return client


def test_tick_reads_from_the_last_position(state):
    client = redis_with(entry(1), entry(2, link="https://example.com/2", title="Iran talks"))
    worker.tick(client, state, NOW)
    client.xadd(worker.STREAM, entry(3, link="https://example.com/3", title="Iran vote")[1], id=entry(3)[0])
    worker.tick(client, state, NOW + 60)
    assert state.store.headline_count() == 3


def test_a_trimmed_position_continues_from_the_oldest(state, caplog):
    state.store.set_last_id("5-0")
    state.store.commit()
    worker.tick(redis_with(entry(1)), state, NOW)
    assert state.store.headline_count() == 1 and "Stats stream lost entries" in caplog.text


def test_redis_outage_keeps_the_last_file(state):
    worker.tick(redis_with(), state, NOW)
    before = state.public.read_text()

    class Broken:
        def xread(self, *a, **k):
            raise ConnectionError("no redis")

        def xinfo_stream(self, *a, **k):
            raise ConnectionError("no redis")

    with pytest.raises(ConnectionError):
        worker.tick(Broken(), state, NOW + 9999)
    assert state.public.read_text() == before


def test_publish_is_atomic(state):
    worker.tick(redis_with(entry()), state, NOW)
    assert sorted(p.name for p in state.public.parent.glob("*.json")) == ["heatmap.json", "stories.json"]
    assert not list(state.public.parent.glob("*.tmp"))
    assert json.loads(state.public.with_name("stories.json").read_text())["KE"][0]["title"] == "Kenya votes"


def test_publishing_waits_for_its_interval(state):
    client = redis_with()
    worker.tick(client, state, NOW)
    worker.tick(client, state, NOW + 60)
    assert json.loads(state.public.read_text())["generated_at"] == NOW


def test_publishing_happens_on_schedule(state):
    client = redis_with()
    worker.tick(client, state, NOW)
    worker.tick(client, state, NOW + 300)
    assert json.loads(state.public.read_text())["generated_at"] == NOW + 300


def test_new_dot_publishes_at_once(state, monkeypatch):
    client = redis_with()
    worker.tick(client, state, NOW)
    client.xadd(worker.STREAM, entry()[1], id=entry()[0])
    dot = {"KE": {"country": "KE", "held": NOW + 60, "since": NOW + 60}}
    monkeypatch.setattr(worker, "red_dots", lambda stories, held, now: dot)
    worker.tick(client, state, NOW + 60)
    assert json.loads(state.public.read_text())["dots"][0]["country"] == "KE"


def test_prune_removes_old_rows(state):
    worker.process([entry(1), entry(2, title="Iran talks", link="https://example.com/2")], state)
    state.store.prune(NOW + 31 * DAY)
    state.store.commit()
    assert stories(state) == [] and state.store.headline_count() == 0


def test_prune_keeps_recent_rows(state):
    worker.process([entry()], state)
    state.store.prune(NOW + 29 * DAY)
    state.store.commit()
    assert len(stories(state)) == 1


def test_recent_stories_return_to_the_index_on_restart(state):
    worker.process([entry()], state)
    fresh = StoryIndex()
    state.store.load_index(fresh, NOW)
    assert fresh.ids == [stories(state)[0].id]


def test_old_vectors_are_not_kept(state):
    worker.process([entry()], state)
    state.store.prune(NOW + 4 * DAY)
    state.store.commit()
    fresh = StoryIndex()
    state.store.load_index(fresh, NOW + 4 * DAY)
    assert fresh.ids == []


def test_empty_database_publishes_an_empty_map(state):
    worker.tick(redis_with(), state, NOW)
    output = json.loads(state.public.read_text())
    assert (output["countries"], output["dots"]) == ({}, [])


def test_dots_survive_a_restart(state):
    state.store.save_dots({"KE": {"country": "KE", "held": NOW, "since": NOW}})
    state.store.commit()
    assert state.store.held_dots()["KE"]["since"] == NOW


def test_backfill_uses_stored_embeddings(state):
    rows = [{"title": "Kenya votes", "source": "Paper", "link": "https://example.com/1", "label": None,
             "first_seen": NOW - DAY, "vector": vector(1)}]
    worker.ingest_rows(rows, state)
    assert stories(state)[0].first_seen == NOW - DAY


def test_each_country_keeps_its_own_position(state):
    state.places = Places({"Kyiv": ["UA", 50.45, 30.52], "Russia": ["RU", None, None]}, {}, {"RU": [55.75, 37.62]}, {})
    worker.process([entry(title="Russia strikes Kyiv")], state)
    assert stories(state)[0].positions == {"RU": (55.75, 37.62), "UA": (50.45, 30.52)}


def test_outlet_home_marks_the_country(state):
    worker.process([entry(title="Budget row", source="Yonhap")], state)
    assert stories(state)[0].countries == {"KR": False}


def test_backfill_skips_rows_without_a_link(state):
    rows = [{"title": "Kenya votes", "source": "Paper", "link": None, "label": None, "first_seen": NOW, "vector": vector(1)}]
    worker.ingest_rows(rows, state)
    assert stories(state) == []


def test_backfill_drops_stories_too_old_to_join(state):
    row = {"title": "Kenya votes", "source": "Paper", "label": None, "vector": vector(1)}
    worker.ingest_rows([{**row, "link": "https://example.com/1", "first_seen": NOW}], state)
    worker.ingest_rows([{**row, "link": "https://example.com/2", "first_seen": NOW + 4 * DAY}], state)
    assert len(state.index.ids) == 1


def test_backfill_drops_unsafe_links(state):
    rows = [{"title": "Kenya votes", "source": "Paper", "link": "javascript:alert(1)", "label": None, "first_seen": NOW, "vector": vector(1)}]
    worker.ingest_rows(rows, state)
    assert state.store.headline_count() == 0


def test_a_failed_save_keeps_the_dots(state):
    state.store.save_dots({"KE": {"country": "KE", "held": NOW, "since": NOW}})
    state.store.commit()
    state.store.db = FailingInsert(state.store.db)
    with pytest.raises(sqlite3.OperationalError):
        state.store.save_dots({})
    state.store.db = state.store.db.real
    state.store.rollback()
    assert "KE" in state.store.held_dots()


class FailingInsert:
    def __init__(self, real):
        self.real = real

    def execute(self, *args):
        return self.real.execute(*args)

    def executemany(self, *args):
        raise sqlite3.OperationalError("disk full")


def test_a_failed_pass_leaves_no_half_saved_dots(state, monkeypatch):
    state.store.save_dots({"KE": {"country": "KE", "held": NOW, "since": NOW}})
    state.store.commit()
    monkeypatch.setattr(worker, "write_json", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    monkeypatch.setattr(worker, "red_dots", lambda stories, held, now: {})
    with pytest.raises(OSError):
        worker.tick(redis_with(), state, NOW)
    assert "KE" in state.store.held_dots()


def test_old_rows_are_pruned_even_when_writing_fails(state, monkeypatch):
    worker.process([entry()], state)
    monkeypatch.setattr(worker, "write_json", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        worker.tick(redis_with(), state, NOW + 31 * DAY)
    assert state.store.headline_count() == 0


def test_a_failed_index_reload_keeps_the_old_index(state, monkeypatch):
    worker.process([entry()], state)
    old = state.index
    monkeypatch.setattr(state.store, "load_index", lambda *a: (_ for _ in ()).throw(RuntimeError("no")))
    with pytest.raises(RuntimeError):
        worker._reset_index(state, NOW)
    assert state.index is old


def test_undecodable_bytes_do_not_stop_the_stream():
    client = worker.connect("redis://example.invalid:6379")
    assert client.get_encoder().encoding_errors == "replace"


def test_the_shared_stream_entry_is_understood():
    sample = json.loads((Path(__file__).parent / "data/stream_entry.json").read_text())
    assert sample["stream"] == worker.STREAM
    item = worker.parse_entry("1800000000000-0", sample["fields"])
    assert item["title"] == "Kenya votes" and item["label"] == "other" and item["when"] == 1800000000.0


def test_a_batch_of_only_unusable_entries_is_an_error(state, caplog):
    worker.process([("1-0", {"title": "x"}), ("2-0", {"title": "y"})], state)
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_a_partly_bad_batch_is_only_a_warning(state, caplog):
    worker.process([("1-0", {"title": "x"}), entry()], state)
    assert not any(r.levelname == "ERROR" for r in caplog.records)
