"""Tests for the tabs as plain text (GET /<view>.txt, /<view>/<topic>.txt), for curl and terminals."""

import json
import time

from catalog import ALL_VIEW, topic_view
from config import settings
from keys import snapshot_key
from routes import plain_text

HOUR = 3600


async def publish(redis_client, view, items, fetched_at=None):
    fetched_at = fetched_at or time.time()
    data = [{"source": "BBC", "published": fetched_at - 2 * HOUR, "last_updated": fetched_at, **item} for item in items]
    await redis_client.set(snapshot_key(view), json.dumps({
        "region": view, "data": data, "fetched_at": fetched_at, "expires_at": fetched_at + settings.REFRESH_INTERVAL_SECONDS}))


async def test_a_tab_reads_as_numbered_headlines_with_their_links(client, mock_redis):
    await publish(mock_redis, ALL_VIEW, [
        {"title": "Fed holds rates - Reuters", "link": "https://www.reuters.com/fed", "source": "Reuters",
         "related": [{"title": "a", "link": "https://a.example/", "source": "A"}, {"title": "b", "link": "https://b.example/", "source": "B"}]},
        {"title": "Show HN: a thing", "link": "https://thing.example/", "source": "Hacker News",
         "comments": "https://news.ycombinator.com/item?id=1"},
    ])
    response = client.get("/all.txt")
    assert response.status_code == 200 and response.headers["content-type"] == "text/plain; charset=utf-8"
    lines = response.text.splitlines()
    assert lines[0].startswith("news.txt · all · updated ")
    assert " 1  Fed holds rates" in lines and "    Reuters · reuters.com · 2h ago · +2 sources" in lines
    assert "    https://www.reuters.com/fed" in lines
    assert " 2  Show HN: a thing" in lines and "    discuss: https://news.ycombinator.com/item?id=1" in lines
    assert "Fed holds rates - Reuters" not in response.text  # Google's " - Publisher", as the page drops it


async def test_a_topic_tab_has_its_own_address(client, mock_redis):
    await publish(mock_redis, topic_view("asia", "tech"), [{"title": "Chip news", "link": "https://chips.example/"}])
    response = client.get("/asia/tech.txt")
    assert response.status_code == 200 and response.text.startswith("news.txt · asia · tech · updated ")
    assert " 1  Chip news" in response.text


async def test_every_tab_lists_the_others(client, mock_redis):
    await publish(mock_redis, "europe", [{"title": "x", "link": "https://x.example/"}])
    text = client.get("/europe.txt").text
    assert "/all.txt" in text and "/asia.txt" in text and "/europe/sport.txt" in text and "https://testserver/" in text


def test_an_unknown_tab_is_404_and_robots_txt_is_still_robots(client):
    assert client.get("/mars.txt").status_code == 404
    assert client.get("/asia/gossip.txt").status_code == 404
    assert client.get("/robots.txt").text.startswith("User-agent: *")


def test_before_the_first_snapshot_it_asks_to_come_back(client):
    response = client.get("/all.txt")
    assert response.status_code == 503 and response.headers["retry-after"] == "10"


async def test_it_is_cached_until_the_next_refresh_like_the_api(client, mock_redis):
    await publish(mock_redis, ALL_VIEW, [{"title": "x", "link": "https://x.example/"}])
    headers = client.get("/all.txt").headers
    assert 0 < int(headers["cache-control"].split("max-age=")[1]) <= settings.REFRESH_INTERVAL_SECONDS
    assert "stale-if-error=86400" in headers["cdn-cache-control"]


async def test_long_titles_are_wrapped_for_an_80_column_terminal(client, mock_redis):
    await publish(mock_redis, ALL_VIEW, [{"title": "word " * 40, "link": "https://x.example/"}])
    lines = client.get("/all.txt").text.splitlines()
    assert all(len(line) <= 80 for line in lines if "://" not in line)
    assert sum(line.startswith("    word") for line in lines) >= 2  # Continued under the title, not the number


# A feed's title reaches a reader's terminal as it is: escape sequences could recolour it, move the
# cursor or (in some terminals) set the window title or a clipboard
async def test_control_characters_never_reach_the_terminal(client, mock_redis):
    await publish(mock_redis, ALL_VIEW, [{"title": "Red\x1b[31m alert\x07\x9b2J\r\nfake line", "link": "https://x.example/\x1b]52;c;x\x07",
                                          "source": "Evil\x1b[0m"}])
    text = client.get("/all.txt").text
    assert not any(ord(c) < 32 and c != "\n" or 0x7f <= ord(c) < 0xa0 for c in text)
    assert " 1  Red[31m alert2J fake line" in text


async def test_a_snapshot_is_written_as_text_once_whatever_the_requests(client, mock_redis, monkeypatch):
    rendered = []
    original = plain_text.render
    monkeypatch.setattr(plain_text, "render", lambda *args: rendered.append(1) or original(*args))
    await publish(mock_redis, ALL_VIEW, [{"title": "x", "link": "https://x.example/"}], fetched_at=time.time() - 10)
    for _ in range(3):
        client.get("/all.txt")
    assert len(rendered) == 1
    await publish(mock_redis, ALL_VIEW, [{"title": "y", "link": "https://y.example/"}])
    monkeypatch.setattr(settings, "LOCAL_CACHE_SECONDS", 0)  # The worker picks up the new snapshot at once
    assert " 1  y" in client.get("/all.txt").text and len(rendered) == 2
