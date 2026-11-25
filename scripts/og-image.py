#!/usr/bin/env python3
"""
Draws the link preview image (caddy/static/og.png, 1200x630: the card a shared link shows in chat
apps and social sites) in the page's light theme: the site's name with a pixel moon, its tabs, and
its description, all from the config folder. Run it again after changing them:

    python scripts/og-image.py [config dir]    # Needs Pillow and the DejaVu Sans Mono font
"""

import sys
import textwrap
from pathlib import Path

import yaml
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
CONFIG = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "config"
OUT = ROOT / "caddy" / "static" / "og.png"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

# The light theme's colours (app/static/retro.css)
PAPER, INK, MUTED, RULE, ACCENT = "#f3efe4", "#1c1a16", "#6f685a", "#cfc8b6", "#a8431a"
MOON, SHADE, CRATER = "#d9a441", "#d3cbb6", "#b8862f"
W, H, MARGIN = 1200, 630, 72


def pixel_moon(draw: ImageDraw.ImageDraw, left: int, top: int, size: int = 16, pixel: int = 9) -> None:
    """A waxing gibbous moon of square pixels, lit on the right, as the header draws it."""
    r = size / 2
    craters = {(9, 4), (10, 4), (11, 9), (6, 10), (7, 10), (12, 12)}
    for y in range(size):
        for x in range(size):
            dx, dy = x + 0.5 - r, y + 0.5 - r
            if dx * dx + dy * dy > r * r:
                continue
            half = (r * r - dy * dy) ** 0.5
            lit = dx > -half * 0.45  # The terminator, a little left of centre
            colour = (CRATER if (x, y) in craters else MOON) if lit else SHADE
            draw.rectangle([left + x * pixel, top + y * pixel, left + (x + 1) * pixel - 1, top + (y + 1) * pixel - 1], fill=colour)
    for sx, sy in ((-3, 2), (size + 2, 1), (-2, size - 3), (size + 3, size - 2)):  # Four stars around it
        draw.rectangle([left + sx * pixel, top + sy * pixel, left + (sx + 1) * pixel - 1, top + (sy + 1) * pixel - 1], fill=MUTED)


def main() -> None:
    site = yaml.safe_load((CONFIG / "site.yaml").read_text())
    regions = ["all"] + [r["id"] for r in yaml.safe_load((CONFIG / "regions.yaml").read_text())["regions"]]
    topics = ["everything"] + [t["id"] for t in yaml.safe_load((CONFIG / "topics.yaml").read_text())["topics"]]

    image = Image.new("RGB", (W, H), PAPER)
    draw = ImageDraw.Draw(image)
    title, tabs, small, body = (ImageFont.truetype(BOLD, 88), ImageFont.truetype(FONT, 34),
                                ImageFont.truetype(FONT, 25), ImageFont.truetype(FONT, 36))

    draw.text((MARGIN, 70), site["name"].upper(), font=title, fill=INK)
    pixel_moon(draw, W - MARGIN - 16 * 9 - 2 * 9, 58)
    draw.rectangle([MARGIN, 214, W - MARGIN, 216], fill=RULE)

    x = MARGIN
    for i, region in enumerate(regions):  # The region tabs, the first one current
        if i:
            draw.text((x, 248), " · ", font=tabs, fill=MUTED)
            x += draw.textlength(" · ", font=tabs)
        draw.text((x, 248), region, font=tabs, fill=ACCENT if i == 0 else INK)
        if i == 0:
            draw.rectangle([x, 292, x + draw.textlength(region, font=tabs), 294], fill=ACCENT)
        x += draw.textlength(region, font=tabs)
    draw.text((MARGIN, 312), " · ".join(topics), font=small, fill=MUTED)

    per_line = int((W - 2 * MARGIN) / draw.textlength("m", font=body))
    for n, line in enumerate(textwrap.wrap(" ".join(site["description"].split()), per_line)[:4]):
        draw.text((MARGIN, 400 + n * 52), line, font=body, fill=INK)

    image.save(OUT, optimize=True)
    print(f"{OUT.relative_to(ROOT)}: {OUT.stat().st_size} bytes")


if __name__ == "__main__":
    main()
