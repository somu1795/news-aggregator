"""Tests for link previews: the card a shared link shows in chat apps and social sites (Open Graph, X's card)."""

import re
import struct
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def meta(page, attribute, name):
    found = re.search(rf'<meta {attribute}="{re.escape(name)}" content="([^"]*)"', page)
    return found and found.group(1)


def page(client, query=""):
    return client.get("/" + query, headers={"Accept": "text/html"}).text


def test_the_page_has_a_preview_card(client):
    html = page(client)
    assert meta(html, "property", "og:title") == "news.txt" and meta(html, "property", "og:site_name") == "news.txt"
    assert meta(html, "property", "og:description").startswith("Headlines from Asia, Europe")
    assert meta(html, "property", "og:url") == "https://testserver/"
    assert meta(html, "property", "og:image") == "https://testserver/og.png"  # Absolute: apps fetch it on their own
    assert meta(html, "property", "og:image:width") == "1200" and meta(html, "property", "og:image:height") == "630"
    assert meta(html, "name", "twitter:card") == "summary_large_image"


def test_a_shared_tab_link_names_its_tab(client):
    html = page(client, "?region=asia&topic=sport")
    assert meta(html, "property", "og:title") == "news.txt · asia · sport"
    assert meta(html, "property", "og:url") == "https://testserver/?region=asia&amp;topic=sport"
    assert meta(page(client, "?region=europe"), "property", "og:title") == "news.txt · europe"


def test_a_made_up_tab_in_a_link_gets_the_plain_card(client):
    html = page(client, '?region="><script>x&topic=gossip')
    assert meta(html, "property", "og:title") == "news.txt" and meta(html, "property", "og:url") == "https://testserver/"
    assert "<script>x" not in html


def test_the_preview_image_is_1200_by_630_and_served_by_caddy():
    image = (REPO / "caddy" / "static" / "og.png").read_bytes()
    assert image[:8] == b"\x89PNG\r\n\x1a\n" and struct.unpack(">II", image[16:24]) == (1200, 630)
    assert len(image) < 300_000  # Apps fetch it on every share; some give up on large images
    icons = next(line for line in (REPO / "caddy" / "Caddyfile").read_text().splitlines() if line.strip().startswith("@icons"))
    assert "/og.png" in icons.split()
