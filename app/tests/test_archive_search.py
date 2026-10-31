"""
Tests for searching the headline archive (services/archive_search.py): which
older headlines a query finds, in what order, as stories, and its typo fallback.
"""

import os
import sqlite3
import threading

import pytest
from pyroaring import BitMap

from config import settings
from services import archive as archive_module
from services import archive_index, archive_search
from services.archive_search import ArchiveUnavailable, search


@pytest.fixture(params=["sqlite", "in memory"])
def archived(request, tmp_path, monkeypatch):
    """
    Archives items given as (title, summary[, published[, story]]), in order of arrival; their links are
    https://pub.example/<n>. Every test runs twice: searched in SQLite, and in the index file written and mapped after.
    """
    monkeypatch.setattr(settings, "ARCHIVE_PATH", str(tmp_path / "archive.db"))
    monkeypatch.setattr(settings, "SEARCH_TYPO_FALLBACK", True)
    monkeypatch.setattr(archive_index, "_index", None)
    monkeypatch.setattr(archive_search, "_kept", threading.local())

    def add(*items, source="Pub"):
        start = len(links())
        archive_module.archive({"all": [
            {"title": item[0], "link": f"https://pub.example/{start + n}", "source": source,
             "published": item[2] if len(item) > 2 and item[2] else 1000.0 + start + n, "summary": item[1],
             "story": item[3] if len(item) > 3 else None}
            for n, item in enumerate(items)]}, 5000.0)
        if request.param == "in memory":
            archive_index.write()
            archive_index.load()
    return add


def links():
    try:
        db = sqlite3.connect(settings.ARCHIVE_PATH)
        return [link for (link,) in db.execute("SELECT link FROM items")]
    except sqlite3.OperationalError:
        return []


def found(query):
    return [item["link"].rsplit("/", 1)[1] for item in search(query)["items"]]


def test_headline_matches_come_first_each_newest_first(archived):
    archived(("Bridge plan approved", None),          # 0
             ("Council meets", "A new bridge plan."),  # 1: only its summary
             ("Bridge closed for repairs", None),      # 2
             ("Weather", None))                        # 3
    assert found("bridge") == ["2", "0", "1"]


def test_newest_means_by_date_published_within_each_group(archived):
    archived(("Bridge plan approved", None, 3000.0),       # 0
             ("Bridge story picked up late", None, 1000.0),  # 1: arrived after 0, published long before
             ("Bridge closed", None, 2000.0),               # 2
             ("Council meets", "A bridge vote.", 500.0),     # 3: only in its summary
             ("Weather", "Rain over the bridge.", 2500.0))   # 4: only in its summary
    assert found("bridge") == ["0", "2", "1", "4", "3"]


def test_every_word_must_start_a_word_case_and_accents_ignored(archived):
    archived(("Election results are in", None), ("Officials said little", None), ("CAFÉ owners protest", None))
    assert found("elect") == ["0"]
    assert found("ai") == []  # Not the "ai" in "said"
    assert found("cafe OWN") == ["2"]
    assert found("café") == ["2"]
    assert found("election weather") == []


def test_punctuation_and_search_syntax_are_just_word_breaks(archived):
    archived(("U.S. economy grows", None), ("Covid-19 cases fall", None), ("Title: a NEAR miss OR not", None))
    assert found("u.s") == ["0"]
    assert found("covid-19") == ["1"]
    for query in ['title: near', '"near', 'near*', 'NEAR(miss', 'miss OR', '{title}: miss', '-miss', '^near']:
        assert found(query) == ["2"], query
    assert found('" * ( ) :') == []  # No words at all


def test_a_misspelled_word_is_searched_as_the_closest_known_word(archived):
    archived(("Ukraine talks resume", None), ("Ukraine grain deal", None), ("Ukrainian music", None))
    result = search("ukrane talks")
    assert [item["link"][-1] for item in result["items"]] == ["0"]
    assert result["corrected"] == [{"from": "ukrane", "to": "ukraine"}]  # The word most items have
    assert result["words"] == ["ukraine", "talks"]  # What the page highlights


def test_words_that_match_something_are_never_corrected(archived):
    archived(("Ukraine talks resume", None))
    assert search("ukr")["corrected"] == []
    assert search("zzzz") == {"items": [], "words": ["zzzz"], "corrected": [], "next": None}  # Nothing close either
    assert search("ukrsine")["corrected"] == [{"from": "ukrsine", "to": "ukraine"}]  # A letter changed


def test_short_words_are_not_corrected(archived):
    archived(("Cat rescued", None))
    assert search("cqt")["corrected"] == []  # Three letters: too many near misses


def test_without_the_typo_fallback_only_prefixes_match(archived, monkeypatch):
    archived(("Ukraine talks resume", None))
    monkeypatch.setattr(settings, "SEARCH_TYPO_FALLBACK", False)
    assert search("ukrane") == {"items": [], "words": ["ukrane"], "corrected": [], "next": None}


def test_results_have_what_the_page_shows(archived):
    archived(("Bridge plan approved", "A summary."))
    assert search("bridge")["items"] == [{
        "title": "Bridge plan approved", "link": "https://pub.example/0", "source": "Pub",
        "published": 1000.0, "summary": "A summary.", "comments": None, "story": None, "related": []}]


def test_at_most_fifty_results(archived):
    archived(*[(f"Bridge story {n}", None) for n in range(60)])
    assert len(found("bridge")) == 50
    assert found("bridge")[0] == "59"


def test_next_asks_for_the_ones_after_until_there_are_none(archived, monkeypatch):
    monkeypatch.setattr(archive_search, "LIMIT", 2)
    archived(("Bridge plan", None), ("Council", "A bridge vote."), ("Bridge closed", None),  # 0, 1, 2
             ("Weather", "Rain on the bridge."), ("Bridge opens", None), ("Roads", "Bridge works."))  # 3, 4, 5
    pages, after = [], None
    while True:
        result = search("bridge", after)
        pages.append([item["link"].rsplit("/", 1)[1] for item in result["items"]])
        after = result["next"]
        if not after:
            break
    assert pages == [["4", "2"], ["0", "5"], ["3", "1"]]  # Headline matches, then the rest, each once


def test_a_page_that_ends_the_headline_matches_says_the_rest_comes_next(archived, monkeypatch):
    monkeypatch.setattr(archive_search, "LIMIT", 2)
    archived(("Bridge plan", None), ("Council", "A bridge vote."), ("Bridge closed", None))
    first = search("bridge")
    assert [item["title"] for item in first["items"]] == ["Bridge closed", "Bridge plan"]
    assert first["next"] == "r"  # The rest, from the newest
    assert [item["title"] for item in search("bridge", "r")["items"]] == ["Council"]
    assert search("bridge", "r")["next"] is None


def test_each_story_once_led_by_its_newest_match_with_its_other_headlines_under_it(archived):
    archived(("Bridge plan approved", None, None, 7),       # 0
             ("City approves crossing", None, None, 7),     # 1: the same story, no match itself
             ("Bridge plan wins vote", None, None, 7),      # 2
             ("Bridge closed for repairs", None, None, 9),  # 3
             ("Council meets", "On the bridge.", None, 7))  # 4: a summary match, in a story already listed
    result = search("bridge")["items"]
    assert [(i["link"][-1], i["story"]) for i in result] == [("3", 9), ("2", 7)]
    assert [r["link"][-1] for r in result[1]["related"]] == ["4", "1", "0"]  # Newest first
    assert result[1]["related"][0] == {"title": "Council meets", "link": "https://pub.example/4", "source": "Pub", "comments": None}


def test_a_story_found_only_in_summaries_is_led_by_its_newest_summary_match(archived):
    archived(("Council meets", "A bridge vote.", None, 3), ("Council votes", "The bridge passes.", None, 3), ("Weather", None, None, 3))
    [story] = search("bridge")["items"]
    assert story["link"][-1] == "1" and [r["link"][-1] for r in story["related"]] == ["2", "0"]


def test_a_story_lists_at_most_twenty_others(archived):
    archived(*[(f"Bridge story {n}", None, None, 1) for n in range(30)])
    [story] = search("bridge")["items"]
    assert len(story["related"]) == 20


def test_pages_count_stories(archived, monkeypatch):
    monkeypatch.setattr(archive_search, "LIMIT", 2)
    archived(("Bridge a", None, None, 1), ("Bridge b", None, None, 2), ("Bridge c", None, None, 1),  # 0, 1, 2
             ("Bridge d", None, None, 3), ("Bridge e", None, None, 3))                             # 3, 4
    first = search("bridge")
    assert [i["link"][-1] for i in first["items"]] == ["4", "2"]  # Stories 3 and 1, by their newest matches
    second = search("bridge", first["next"])
    # Story 2, then story 1 again by an older match: a page doesn't know the ones before it (the page merges them)
    assert [(i["link"][-1], i["story"]) for i in second["items"]] == [("1", 2), ("0", 1)] and second["next"] is None


def test_without_an_archive_search_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ARCHIVE_PATH", "")
    with pytest.raises(ArchiveUnavailable):
        search("bridge")
    monkeypatch.setattr(settings, "ARCHIVE_PATH", str(tmp_path / "missing.db"))
    with pytest.raises(ArchiveUnavailable):
        search("bridge")
    assert not (tmp_path / "missing.db").exists()  # Never created by a reader


def test_an_archive_not_yet_indexed_is_unavailable(tmp_path, monkeypatch):
    path = tmp_path / "archive.db"
    sqlite3.connect(path).executescript(archive_module.SCHEMA.split("CREATE VIRTUAL TABLE")[0])
    monkeypatch.setattr(settings, "ARCHIVE_PATH", str(path))
    with pytest.raises(ArchiveUnavailable):
        search("bridge")


def test_the_reader_never_writes(archived):
    archived(("Bridge plan approved", None))
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        archive_search._connect().execute("DELETE FROM items")


def test_a_new_index_file_replaces_the_old_one_which_stays_readable(archived, monkeypatch):
    archived(("Bridge plan approved", None))
    archive_index.write()
    first = archive_index.load()
    archived(("Bridge closed", None))
    assert archive_index.write()["headlines"] == 2
    second = archive_index.load()
    assert archive_index.current() is second
    assert [i["title"] for i in search("bridge")["items"]] == ["Bridge closed", "Bridge plan approved"]
    monkeypatch.setattr(archive_index, "_index", first)  # A worker that hasn't mapped the new file yet
    assert [i["title"] for i in search("bridge")["items"]] == ["Bridge plan approved"]  # Renamed over, not overwritten


def test_an_index_file_is_read_without_building_anything(archived):
    archived(("Bridge plan approved", "On the river."), ("Council meets", None))
    archive_index.write()
    index = archive_index.Index(archive_index.index_path())
    assert index.headlines == 2 and "bridge" in index.words
    assert index.prefix("b")[0] == BitMap([1]) and index.prefix("bri")[1] == BitMap([1])
    assert index.prefix("rive") == (BitMap([1]), BitMap())  # Only in a summary
    assert index.word("bridg") == (BitMap(), BitMap()) and index.known({"bridge", "council"}) == "bridge"


def test_a_damaged_index_file_is_refused_and_searches_go_on(archived):
    archived(("Bridge plan approved", "On the river."), ("Council meets", None))
    archive_index.write()
    path = archive_index.index_path()
    whole = open(path, "rb").read()
    for cut in (len(whole) - 1, len(whole) - 8, len(whole) // 2, 100):  # Cut short anywhere
        open(path, "wb").write(whole[:cut])
        with pytest.raises(ValueError):
            archive_index.Index(path)
    assert [i["title"] for i in search("bridge")["items"]] == ["Bridge plan approved"]  # The index it had, or SQLite


def test_a_failed_write_leaves_the_previous_index_and_no_temporary_file(archived, monkeypatch):
    archived(("Bridge plan approved", None))
    archive_index.write()
    before = open(archive_index.index_path(), "rb").read()

    def disk_full(*args):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(archive_index, "_write_sections", disk_full)
    with pytest.raises(OSError):
        archive_index.write()
    assert open(archive_index.index_path(), "rb").read() == before
    assert not os.path.exists(archive_index.index_path() + ".tmp")
