"""Tests for finding the countries a headline is about."""

from pathlib import Path

import pytest

from places import Places, clean_title


@pytest.fixture(scope="module")
def places():
    return Places.load(Path(__file__).parent / "data")


def countries(places, title):
    return [hit.country for hit in places.detect(title)]


def test_longest_name_wins(places):
    assert countries(places, "Papua New Guinea floods") == ["PG"]


def test_georgia_is_not_placed(places):
    assert countries(places, "Georgia voters head to polls") == []


def test_jordan_needs_a_capital_letter(places):
    assert countries(places, "jordan shoes on sale") == []


def test_turkey_as_a_name(places):
    assert countries(places, "Turkey quake kills 10") == ["TR"]


def test_order_and_limit(places):
    assert countries(places, "US strikes Iran as Russia and China object") == ["US", "IR"]


def test_repeated_country_counts_once(places):
    assert countries(places, "Lagos floods hit Nigeria") == ["NG"]


def test_abbreviation_is_case_sensitive(places):
    assert countries(places, "us and them") == []


def test_outlet_fallback(places):
    assert places.locate("Budget minister vows review", "Yonhap").countries == ["KR"]


def test_outlet_fallback_is_marked(places):
    assert places.locate("Budget minister vows review", "Yonhap").from_title is False


def test_title_wins_over_the_outlet(places):
    located = places.locate("Iran talks resume", "Yonhap")
    assert located.countries == ["IR"] and located.from_title is True


def test_global_outlet_has_no_home(places):
    assert places.locate("Budget minister vows review", "Al Jazeera").countries == []


def test_city_gives_coordinates(places):
    assert places.locate("Quake hits Lagos", "x").lat == pytest.approx(6.45, abs=0.5)


def test_country_alone_gives_its_capital(places):
    assert places.locate("Kenya votes", "x").lat == pytest.approx(-1.29, abs=0.5)


def test_entities_are_unescaped():
    assert clean_title("Surgeries in Zamb&#xe9;zia") == "Surgeries in Zambézia"


def test_spaces_are_tidied():
    assert clean_title("  A   headline\n") == "A headline"
