"""
Tests for searching the headline archive (services/archive_search.py): which
older headlines a query finds, in what order, and its typo fallback.
"""

import sqlite3

import pytest

from config import settings
from services import archive as archive_module
from services import archive_search
from services.archive_search import ArchiveUnavailable, search


@pytest.fixture
def archived(tmp_path, monkeypatch):
    """Archives items given as (title, summary[, published]), in order of arrival; their links are https://pub.example/<n>."""
    monkeypatch.setattr(settings, "ARCHIVE_PATH", str(tmp_path / "archive.db"))
    monkeypatch.setattr(settings, "SEARCH_TYPO_FALLBACK", True)

    def add(*items, source="Pub"):
        start = len(links())
        archive_module.archive({"all": [
            {"title": item[0], "link": f"https://pub.example/{start + n}", "source": source,
             "published": item[2] if len(item) > 2 else 1000.0 + start + n, "summary": item[1]}
            for n, item in enumerate(items)]}, 5000.0)
    return add


def links():
    try:
        db = sqlite3.connect(settings.ARCHIVE_PATH)
        return [link for (link,) in db.execute("SELECT link FROM items")]
    except sqlite3.OperationalError:
        return []


def found(query, sort="newest"):
    return [item["link"].rsplit("/", 1)[1] for item in search(query, sort)["items"]]


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
    result = search("ukrane talks", "newest")
    assert [item["link"][-1] for item in result["items"]] == ["0"]
    assert result["corrected"] == [{"from": "ukrane", "to": "ukraine"}]  # The word most items have
    assert result["words"] == ["ukraine", "talks"]  # What the page highlights


def test_words_that_match_something_are_never_corrected(archived):
    archived(("Ukraine talks resume", None))
    assert search("ukr", "newest")["corrected"] == []
    assert search("zzzz", "newest") == {"items": [], "words": ["zzzz"], "corrected": []}  # Nothing close either
    assert search("ukrsine", "newest")["corrected"] == [{"from": "ukrsine", "to": "ukraine"}]  # A letter changed


def test_short_words_are_not_corrected(archived):
    archived(("Cat rescued", None))
    assert search("cqt", "newest")["corrected"] == []  # Three letters: too many near misses


def test_without_the_typo_fallback_only_prefixes_match(archived, monkeypatch):
    archived(("Ukraine talks resume", None))
    monkeypatch.setattr(settings, "SEARCH_TYPO_FALLBACK", False)
    assert search("ukrane", "newest") == {"items": [], "words": ["ukrane"], "corrected": []}


def test_best_match_ranks_headline_matches_and_short_headlines_first(archived):
    archived(("Rover update", None),                                                 # 0: oldest, a short headline
             ("Mars rover finds water", None),                                       # 1
             ("Space news roundup", "Mars, the Moon, and a rover's water finding."),  # 2: newest, in the summary only
             *[(f"Other story {n}", None) for n in range(20)])                       # So the words are rare
    assert found("rover", "newest") == ["1", "0", "2"]
    assert found("rover", "best") == ["0", "1", "2"]  # The headline most about it first
    assert found("mars water", "best") == ["1", "2"]


def test_results_have_what_the_page_shows(archived):
    archived(("Bridge plan approved", "A summary."))
    assert search("bridge", "newest")["items"] == [{
        "title": "Bridge plan approved", "link": "https://pub.example/0", "source": "Pub",
        "published": 1000.0, "summary": "A summary.", "comments": None}]


def test_at_most_fifty_results(archived):
    archived(*[(f"Bridge story {n}", None) for n in range(60)])
    assert len(found("bridge")) == 50
    assert found("bridge")[0] == "59"


def test_without_an_archive_search_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ARCHIVE_PATH", "")
    with pytest.raises(ArchiveUnavailable):
        search("bridge", "newest")
    monkeypatch.setattr(settings, "ARCHIVE_PATH", str(tmp_path / "missing.db"))
    with pytest.raises(ArchiveUnavailable):
        search("bridge", "newest")
    assert not (tmp_path / "missing.db").exists()  # Never created by a reader


def test_an_archive_not_yet_indexed_is_unavailable(tmp_path, monkeypatch):
    path = tmp_path / "archive.db"
    sqlite3.connect(path).executescript(archive_module.SCHEMA.split("CREATE VIRTUAL TABLE")[0])
    monkeypatch.setattr(settings, "ARCHIVE_PATH", str(path))
    with pytest.raises(ArchiveUnavailable):
        search("bridge", "newest")


def test_the_reader_never_writes(archived):
    archived(("Bridge plan approved", None))
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        archive_search._connect().execute("DELETE FROM items")
