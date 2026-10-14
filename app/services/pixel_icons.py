"""
Pixel-art site icons, for websites the icon service has none for: the
website's own icon redrawn as 16×16 pixel art in its own colours
(services/site_icons.py finds the icon). Served as an SVG of square pixels,
so it stays sharp at any size.

Each pixel is decided from a 6×6 block of a 96×96 copy of the icon: the
block's average colour, matched to the icon's 8 main colours (no blended
in-between shades). A block that is mostly the icon's background but crossed
by a line takes the line's colour, so thin strokes survive, as a pixel
artist would draw them.
"""

import io
from collections import Counter
from typing import Dict, List, Optional, Tuple

from PIL import Image

GRID = 16
CELL = 6
COLOURS = 8
STROKE_SHARE = 0.25  # Of a block, for a line crossing the background to take it
FORMATS = ["ICO", "PNG", "JPEG", "GIF", "WEBP", "BMP"]  # Raster icons only; nothing else is decoded
MAX_PIXELS = 2048 * 2048  # Larger pictures are refused (Pillow raises), whatever the file size

RGB = Tuple[int, int, int]


def load(data: bytes) -> Image.Image:
    """The icon as RGBA, the largest picture of an .ico. Raises on anything else or anything too large."""
    im = Image.open(io.BytesIO(data), formats=FORMATS)
    if im.format == "ICO":
        im.size = max(im.ico.sizes())
    if im.width * im.height > MAX_PIXELS:
        raise ValueError("picture too large")
    return im.convert("RGBA")


def _square(im: Image.Image) -> Image.Image:
    """Trimmed to what's visible, centred on a transparent square."""
    bbox = im.getchannel("A").point(lambda a: 255 if a > 24 else 0).getbbox()
    if bbox:
        im = im.crop(bbox)
    side = max(im.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(im, ((side - im.width) // 2, (side - im.height) // 2))
    return square


def _resize(im: Image.Image, side: int, resample) -> Image.Image:
    return im.convert("RGBa").resize((side, side), resample).convert("RGBA")  # Premultiplied: no dark fringes


def _palette(pixels: List[RGB]) -> List[RGB]:
    """The main colours, near-duplicates merged."""
    sample = Image.new("RGB", (len(pixels), 1))
    sample.putdata(pixels)
    flat = sample.quantize(colors=COLOURS, method=Image.Quantize.MEDIANCUT).getpalette()[:COLOURS * 3]
    palette: List[RGB] = []
    for colour in zip(flat[0::3], flat[1::3], flat[2::3]):
        if all(sum((a - b) ** 2 for a, b in zip(colour, kept)) > 900 for kept in palette):
            palette.append(colour)
    return palette


def pixelate(data: bytes) -> Optional[Dict[Tuple[int, int], RGB]]:
    """The icon as GRID×GRID pixels ({(x, y): colour}, transparent ones left out), or None if nothing is visible."""
    square = _square(load(data))
    side = GRID * CELL
    detail = _resize(square, side, Image.NEAREST if square.width < side else Image.BOX)
    averaged = list(_resize(detail, GRID, Image.BOX).getdata())
    source = list(detail.getdata())
    opaque = [p[:3] for p in source if p[3] >= 128]
    if not opaque:
        return None
    palette = _palette(opaque)
    nearest_cache: Dict[RGB, int] = {}

    def nearest(colour: RGB) -> int:  # Weighted towards green, as the eye is
        if colour not in nearest_cache:
            nearest_cache[colour] = min(range(len(palette)), key=lambda i: sum(
                (a - b) ** 2 * w for a, b, w in zip(colour, palette[i], (2, 4, 3))))
        return nearest_cache[colour]

    index = [-1 if p[3] < 128 else nearest(p[:3]) for p in source]  # -1: transparent
    edge = index[:side] + index[-side:] + index[::side] + index[side - 1::side]
    background = Counter(edge).most_common(1)[0][0]
    pixels: Dict[Tuple[int, int], RGB] = {}
    for y in range(GRID):
        for x in range(GRID):
            block = Counter(index[(y * CELL + dy) * side + x * CELL + dx] for dy in range(CELL) for dx in range(CELL))
            choice = None
            if block.most_common(1)[0][0] == background:
                lines = [(n, i) for i, n in block.items() if i != background and n >= STROKE_SHARE * CELL * CELL]
                if lines:
                    choice = max(lines)[1]
            if choice is None:
                r, g, b, a = averaged[y * GRID + x]
                choice = -1 if a < 128 else nearest((r, g, b))
            if choice >= 0:
                pixels[x, y] = palette[choice]
    return pixels


def to_svg(pixels: Dict[Tuple[int, int], RGB]) -> bytes:
    """One path per colour, each row's runs as rectangles."""
    paths: Dict[RGB, List[str]] = {}
    for y in range(GRID):
        x = 0
        while x < GRID:
            colour = pixels.get((x, y))
            end = x + 1
            while end < GRID and pixels.get((end, y)) == colour:
                end += 1
            if colour:
                paths.setdefault(colour, []).append(f"M{x} {y}h{end - x}v1h-{end - x}z")
            x = end
    body = "".join(f'<path fill="#{r:02x}{g:02x}{b:02x}" d="{"".join(d)}"/>' for (r, g, b), d in paths.items())
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {GRID} {GRID}" width="{GRID}" height="{GRID}" '
            f'shape-rendering="crispEdges">{body}</svg>').encode()


def pixel_art(data: bytes) -> Optional[bytes]:
    """The icon as a pixel-art SVG; None if it can't be read or shows nothing."""
    try:
        pixels = pixelate(data)
    except Exception:  # Not an image we decode, damaged, or too large
        return None
    return to_svg(pixels) if pixels else None


def picture_size(data: bytes) -> int:
    """The icon's largest side in pixels (its largest picture, for an .ico); 0 if it can't be read or is too large."""
    try:
        im = Image.open(io.BytesIO(data), formats=FORMATS)
        width, height = max(im.ico.sizes()) if im.format == "ICO" else im.size
    except Exception:
        return 0
    return max(width, height) if width * height <= MAX_PIXELS else 0
