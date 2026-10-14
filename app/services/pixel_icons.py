"""
Stand-in site icons: a 16×16 pixel-art tile with the website's initial, for
websites whose own icon couldn't be fetched (services/site_icons.py). Drawn
here, not fetched: the same website always gets the same tile and colour.
"""

import hashlib
from typing import Dict, List, Tuple

# 8×10 glyphs, 2-pixel strokes so they survive being shown at 14–15 px
_GLYPHS = """
A ..####.. .######. ##....## ##....## ######## ######## ##....## ##....## ##....## ##....##
B #######. ######## ##....## ##...##. #######. ######## ##....## ##....## ######## #######.
C .####### ######## ##...... ##...... ##...... ##...... ##...... ##...... ######## .#######
D ######.. #######. ##...### ##....## ##....## ##....## ##....## ##...### #######. ######..
E ######## ######## ##...... ##...... ######.. ######.. ##...... ##...... ######## ########
F ######## ######## ##...... ##...... ######.. ######.. ##...... ##...... ##...... ##......
G .####### ######## ##...... ##...... ##..#### ##..#### ##....## ##....## ######## .######.
H ##....## ##....## ##....## ##....## ######## ######## ##....## ##....## ##....## ##....##
I ###### ###### ..##.. ..##.. ..##.. ..##.. ..##.. ..##.. ###### ######
J ..###### ..###### ......## ......## ......## ......## ##....## ##....## ######## .######.
K ##....## ##...##. ##..##.. ##.##... #####... #####... ##.##... ##..##.. ##...##. ##....##
L ##...... ##...... ##...... ##...... ##...... ##...... ##...... ##...... ######## ########
M ##....## ###..### ######## ##.##.## ##....## ##....## ##....## ##....## ##....## ##....##
N ##....## ##....## ###...## ####..## ##.##.## ##..#### ##...### ##....## ##....## ##....##
O .######. ######## ##....## ##....## ##....## ##....## ##....## ##....## ######## .######.
P #######. ######## ##....## ##....## ######## #######. ##...... ##...... ##...... ##......
Q .######. ######## ##....## ##....## ##....## ##....## ##..#### ##...##. #######. .####.##
R #######. ######## ##....## ##....## ######## #######. ##..##.. ##...##. ##....## ##....##
S .####### ######## ##...... ##...... #######. .####### ......## ......## ######## #######.
T ######## ######## ...##... ...##... ...##... ...##... ...##... ...##... ...##... ...##...
U ##....## ##....## ##....## ##....## ##....## ##....## ##....## ##....## ######## .######.
V ##....## ##....## ##....## ##....## ##....## .##..##. .##..##. ..####.. ..####.. ...##...
W ##....## ##....## ##....## ##....## ##.##.## ##.##.## ######## ######## ###..### ##....##
X ##....## ##....## .##..##. ..####.. ...##... ...##... ..####.. .##..##. ##....## ##....##
Y ##....## ##....## .##..##. ..####.. ...##... ...##... ...##... ...##... ...##... ...##...
Z ######## ######## .....##. ....##.. ...##... ..##.... .##..... ##...... ######## ########
0 .######. ######## ##...### ##..#### ##.##.## ##.##.## ####..## ###...## ######## .######.
1 ..##.. .###.. ####.. ..##.. ..##.. ..##.. ..##.. ..##.. ###### ######
2 .######. ######## ......## ......## .####### #######. ##...... ##...... ######## ########
3 #######. ######## ......## ......## ..###### ..###### ......## ......## ######## #######.
4 ##....## ##....## ##....## ##....## ######## ######## ......## ......## ......## ......##
5 ######## ######## ##...... ##...... #######. ######## ......## ......## ######## #######.
6 .####### ######## ##...... ##...... #######. ######## ##....## ##....## ######## .######.
7 ######## ######## ......## .....##. ....##.. ...##... ...##... ...##... ...##... ...##...
8 .######. ######## ##....## ##....## .######. ######## ##....## ##....## ######## .######.
9 .######. ######## ##....## ##....## ######## .####### ......## ......## ######## #######.
"""
GLYPHS: Dict[str, List[str]] = {line[0]: line[2:].split() for line in _GLYPHS.strip().splitlines()}

# Muted tile colours that sit with the page's paper and amber themes; cream letters on each
PALETTE = ["#a8431a", "#2f6f6a", "#5d6b22", "#3f5873", "#6d3f6b", "#8a5a12", "#2f6b3f", "#8a2f3a", "#4a4a8c", "#7a4a2a"]
LETTER = "#f8f2e2"
SIZE = 16
CORNERS = {(0, 0), (1, 0), (0, 1), (14, 0), (15, 0), (15, 1), (0, 14), (0, 15), (1, 15), (15, 14), (14, 15), (15, 15)}
# Common second-level parts of country domains (bbc.co.uk, espn.com.au), skipped to reach the name
SECOND_LEVEL = {"ac", "co", "com", "edu", "gov", "net", "org", "or", "ne", "go"}


def site_name(host: str) -> str:
    """The website's name in its domain: "espn" for africa.espn.com, "kyodonews" for english.kyodonews.net."""
    labels = host.split(".")[:-1]  # Without the top-level domain
    if len(labels) > 1 and labels[-1] in SECOND_LEVEL:
        labels = labels[:-1]
    return labels[-1] if labels else host


def _shade(colour: str, factor: float) -> str:
    return "#" + "".join(f"{int(int(colour[i:i + 2], 16) * factor):02x}" for i in (1, 3, 5))


def pixel_icon(host: str) -> bytes:
    """The SVG tile for a website: its initial, in a colour picked by its name."""
    name = site_name(host)
    initial = next((c for c in name.upper() if c in GLYPHS), "N")
    tile = PALETTE[int(hashlib.sha256(name.encode()).hexdigest(), 16) % len(PALETTE)]
    edge, shadow = _shade(tile, 0.72), _shade(tile, 0.5)

    inside = {(x, y) for y in range(SIZE) for x in range(SIZE)} - CORNERS
    # A darker edge where the tile ends to the right or below: lit from the top left
    pixels: Dict[Tuple[int, int], str] = {
        (x, y): edge if (x + 1, y) not in inside or (x, y + 1) not in inside else tile for x, y in inside}
    glyph = GLYPHS[initial]
    left, top = (SIZE - len(glyph[0])) // 2, 3
    for colour, dx in ((shadow, 1), (LETTER, 0)):  # A drop shadow one pixel down and right, then the letter
        for gy, row in enumerate(glyph):
            for gx, cell in enumerate(row):
                if cell == "#":
                    pixels[left + gx + dx, top + gy + dx] = colour

    # One path per colour, each row's runs as rectangles
    paths: Dict[str, List[str]] = {}
    for y in range(SIZE):
        x = 0
        while x < SIZE:
            colour = pixels.get((x, y))
            end = x + 1
            while end < SIZE and pixels.get((end, y)) == colour:
                end += 1
            if colour:
                paths.setdefault(colour, []).append(f"M{x} {y}h{end - x}v1h-{end - x}z")
            x = end
    body = "".join(f'<path fill="{colour}" d="{"".join(d)}"/>' for colour, d in paths.items())
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SIZE} {SIZE}" width="{SIZE}" height="{SIZE}" '
            f'shape-rendering="crispEdges">{body}</svg>').encode()
