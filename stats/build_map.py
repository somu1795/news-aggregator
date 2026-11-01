"""
Builds caddy/static/stats/map.json, the map's grid of cells. Run by hand, not in the service:

    python build_map.py <ne_50m_admin_0_countries.geojson> <world-countries countries.json>

A cell belongs to the country its centre is in (Natural Earth, public domain). A country
too small to hold a centre takes the cell at its position, so everything placed can be shown.
"""

import json
import sys
from pathlib import Path

from shapely.geometry import Point, shape
from shapely.prepared import prep

WIDTH, HEIGHT = 180, 80
NORTH, SOUTH = 83.0, -58.0  # Antarctica has no news of its own here
CROWDED = 3  # A small country may take a cell from a country with more than this many


def cell_of(lat, lon):
    x = min(WIDTH - 1, max(0, int((lon + 180) / 360 * WIDTH)))
    y = min(HEIGHT - 1, max(0, int((NORTH - lat) / (NORTH - SOUTH) * HEIGHT)))
    return x, y


def cell_centre(x, y):
    return Point(-180 + (x + 0.5) * 360 / WIDTH, NORTH - (y + 0.5) * (NORTH - SOUTH) / HEIGHT)


def main(shapes_path, countries_path):
    here = Path(__file__).parent
    places = json.loads((here / "data/places.json").read_text())
    homes = json.loads((here / "data/homes.json").read_text())
    wanted = {t[0] for t in places["terms"].values()} | set(places["abbreviations"].values()) | set(homes.values())
    names = {c["cca2"]: c["name"]["common"] for c in json.load(open(countries_path, encoding="utf8"))}

    shapes = {}
    for feature in json.load(open(shapes_path, encoding="utf8"))["features"]:
        code = feature["properties"]["ISO_A2_EH"]
        if len(code) == 2:
            shapes[code] = shape(feature["geometry"])
    cells = {}
    prepared = {code: (prep(geometry), geometry.bounds) for code, geometry in shapes.items()}
    for y in range(HEIGHT):
        for x in range(WIDTH):
            centre = cell_centre(x, y)
            for code, (geometry, (west, south, east, north)) in prepared.items():
                if west <= centre.x <= east and south <= centre.y <= north and geometry.contains(centre):
                    cells[(x, y)] = code
                    break

    def count(code):
        return sum(1 for c in cells.values() if c == code)

    for code in sorted(wanted | set(shapes)):
        if count(code):
            continue
        if code in shapes:
            point = shapes[code].representative_point()
            lat, lon = point.y, point.x
        elif code in places["capitals"]:
            lat, lon = places["capitals"][code]
        else:
            continue
        x, y = cell_of(lat, lon)
        spot = next(((x + dx, y + dy) for dx, dy in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1), (1, -1), (-1, 1))
                     if 0 <= x + dx < WIDTH and 0 <= y + dy < HEIGHT and
                     ((x + dx, y + dy) not in cells or count(cells[(x + dx, y + dy)]) > CROWDED)), None)
        if spot:
            cells[spot] = code

    shown = set(cells.values())
    output = {"width": WIDTH, "height": HEIGHT, "north": NORTH, "south": SOUTH,
              "names": {code: names.get(code, code) for code in sorted(shown)},
              "cells": [[x, y, code] for (x, y), code in sorted(cells.items())]}
    path = here / "../caddy/static/stats/map.json"
    path.write_text(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
    print(f"{len(cells)} cells, {len(shown)} countries, missing {sorted(wanted - shown)}")


if __name__ == "__main__":
    main(*sys.argv[1:])
