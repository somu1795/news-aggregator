"""Tests for the map's grid of cells (caddy/static/stats/map.json, from build_map.py)."""

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parent


@pytest.fixture(scope="module")
def grid():
    return json.loads((ROOT / "../caddy/static/stats/map.json").read_text())


def placed_countries():
    data = json.loads((ROOT / "data/places.json").read_text())
    homes = json.loads((ROOT / "data/homes.json").read_text())
    return {t[0] for t in data["terms"].values()} | set(data["abbreviations"].values()) | set(homes.values())


def test_grid_size(grid):
    assert (grid["width"], grid["height"]) == (180, 80)


def test_every_placed_country_has_a_cell(grid):
    assert placed_countries() - {c for _, _, c in grid["cells"]} == set()


def test_every_placed_country_has_a_name(grid):
    assert placed_countries() - set(grid["names"]) == set()


def test_cells_are_inside_the_grid(grid):
    assert all(0 <= x < grid["width"] and 0 <= y < grid["height"] for x, y, _ in grid["cells"])


def test_no_cell_is_listed_twice(grid):
    assert len({(x, y) for x, y, _ in grid["cells"]}) == len(grid["cells"])


def test_cell_codes_are_country_codes(grid):
    assert all(len(c) == 2 and c.isupper() for _, _, c in grid["cells"])


def test_big_countries_keep_their_shape(grid):
    counts = {}
    for _, _, c in grid["cells"]:
        counts[c] = counts.get(c, 0) + 1
    assert counts["RU"] > 300 and counts["US"] > 100 and counts["KE"] >= 4


def test_small_places_are_not_lost(grid):
    assert {"SG", "HK", "PS", "XK"} <= {c for _, _, c in grid["cells"]}
