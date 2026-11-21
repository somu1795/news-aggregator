"""
Tests for the background refresher: per-region views and the "all" view,
link resolution, publishing, and keeping previous snapshots when a refresh
comes back empty.
"""

import json
import os
import sqlite3
import time
from types import SimpleNamespace

import httpx
import pytest
import respx

import catalog as catalog_module
from catalog import ALL_VIEW, Catalog
from config import settings
from keys import snapshot_key, subject_key, topic_check_key, topic_checks_pending_key
import refresher
from refresher import refresh_once, sync_stories, until_next_cycle, write_search_index
from services import archive, archive_index, stats_stream

CATALOG = Catalog(
    regions=[
        {"id": "asia", "label": "Asia", "sources": [
            {"name": "GN India", "url": "https://news.google.com/rss?ceid=IN:en", "weight": 0.6},
            {"name": "Outlet Asia", "url": "https://asia.example/rss", "weight": 0.4},
        ]},
        {"id": "europe", "label": "Europe", "sources": [
            {"name": "GN UK", "url": "https://news.google.com/rss?ceid=GB:en", "weight": 1.0},
        ]},
    ],
    extras=[{"name": "Hacker News", "url": "https://news.ycombinator.com/rss", "weight": 0.2}],
)


def rss_date(seconds_ago):
    return time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime(time.time() - seconds_ago))


def google_feed(prefix, count):
    """A Google News feed: Google links, a different publisher per item."""
    return "<rss><channel>" + "".join(
        f"""<item><title>{prefix} story {i}</title>
            <link>https://news.google.com/rss/articles/{prefix}-{i}</link>
            <pubDate>{rss_date(i * 60)}</pubDate>
            <source url="https://{prefix}-{i}.example">{prefix} Publisher {i}</source></item>"""
        for i in range(count)
    ) + "</channel></rss>"


def outlet_feed(domain, count, comments=False):
    """A single outlet's (or HN's) feed: direct links, no Google resolution needed."""
    return "<rss><channel>" + "".join(
        f"""<item><title>{domain} story {i}</title><link>https://{domain}/a/{i}</link>
            <pubDate>{rss_date(i * 60)}</pubDate>"""
        + (f"<comments>https://news.ycombinator.com/item?id={i}</comments>" if comments else "")
        + "</item>"
        for i in range(count)
    ) + "</channel></rss>"


@pytest.fixture(autouse=True)
def catalog(monkeypatch):
    monkeypatch.setattr(catalog_module, "_catalog", CATALOG)
    monkeypatch.setattr(settings, "MAX_HEADLINES", 20)


def mock_feeds():
    respx.get("https://news.google.com/rss?ceid=IN:en").mock(return_value=httpx.Response(200, content=google_feed("in", 30)))
    respx.get("https://news.google.com/rss?ceid=GB:en").mock(return_value=httpx.Response(200, content=google_feed("gb", 30)))
    respx.get("https://asia.example/rss").mock(return_value=httpx.Response(200, content=outlet_feed("asia.example", 30)))
    respx.get("https://news.ycombinator.com/rss").mock(
        return_value=httpx.Response(200, content=outlet_feed("hn.example", 30, comments=True)))
    respx.get(url__startswith="https://news.google.com/rss/articles/").mock(
        side_effect=lambda request: httpx.Response(
            302, headers={"Location": f"https://publisher.example/{request.url.path.split('/')[-1]}"},
        )
    )
    respx.get(url__startswith="https://publisher.example/").mock(return_value=httpx.Response(200))


async def read_view(redis, view):
    return json.loads(await redis.get(snapshot_key(view)))


async def refresh(redis, poller):
    """One refresh cycle; returns the number of main views published."""
    async with httpx.AsyncClient() as http_client:
        return await refresh_once(http_client, redis, poller)


@respx.mock
async def test_refresh_publishes_every_view(mock_redis, poller):
    mock_feeds()

    assert await refresh(mock_redis, poller) == 3  # all, asia, europe

    asia = await read_view(mock_redis, "asia")
    europe = await read_view(mock_redis, "europe")
    everything = await read_view(mock_redis, ALL_VIEW)
    assert asia["region"] == "asia" and europe["region"] == "europe" and everything["region"] == ALL_VIEW
    assert everything["expires_at"] == pytest.approx(everything["fetched_at"] + settings.REFRESH_INTERVAL_SECONDS)

    # Every view has the same mix: 80% news (that region's, or all regions equally), 20% HN; each
    # cycle's mix starts at a random point (any source can lead), so a count can be one off
    asia_titles = [h["title"] for h in asia["data"]]
    assert len(asia_titles) == 20
    assert abs(sum(t.startswith("hn.example") for t in asia_titles) - 4) <= 1
    assert abs(sum(t.startswith("in ") for t in asia_titles) - 16 * 0.6) <= 2  # Region weights within the news share
    assert abs(sum(t.startswith(("in ", "asia.example")) for t in asia_titles) - 16) <= 1
    assert abs(sum(t.startswith("gb ") for t in (h["title"] for h in europe["data"])) - 16) <= 1

    all_titles = [h["title"] for h in everything["data"]]
    assert len(all_titles) == 20
    assert abs(sum(t.startswith("hn.example") for t in all_titles) - 4) <= 1
    assert abs(sum(t.startswith(("in ", "asia.example")) for t in all_titles) - 8) <= 1
    assert abs(sum(t.startswith("gb ") for t in all_titles) - 8) <= 1

    # Google links resolved (in every view), direct links and HN discussion links untouched
    for view in (asia, europe, everything):
        assert not any("news.google.com" in h["link"] for h in view["data"])
    hn = [h for h in everything["data"] if h["title"].startswith("hn.example")]
    assert hn[0]["link"].startswith("https://hn.example/") and hn[0]["comments"].startswith("https://news.ycombinator.com/item")


@respx.mock
async def test_single_outlet_feed_is_not_capped_in_its_region(mock_redis, poller):
    """An outlet's own feed (all items from one publisher) fills its weight; the cap would allow only 3."""
    mock_feeds()
    await refresh(mock_redis, poller)
    asia = await read_view(mock_redis, "asia")
    everything = await read_view(mock_redis, ALL_VIEW)
    assert abs(sum("asia.example" in h["link"] for h in asia["data"]) - 16 * 0.4) <= 1
    # In "all" it comes via the multi-publisher Asia mix, so the per-publisher cap applies
    assert sum("asia.example" in h["link"] for h in everything["data"]) == settings.MAX_PER_PUBLISHER


@respx.mock
async def test_empty_view_keeps_its_previous_snapshot(mock_redis, poller, monkeypatch):
    monkeypatch.setattr(settings, "FETCH_RETRIES", 1)
    mock_feeds()
    respx.get("https://news.google.com/rss?ceid=GB:en").mock(return_value=httpx.Response(500))
    await mock_redis.set(snapshot_key("europe"), "previous europe")

    assert await refresh(mock_redis, poller) == 2  # all, asia

    assert await mock_redis.get(snapshot_key("europe")) == "previous europe"
    assert (await read_view(mock_redis, "asia"))["data"]


@respx.mock
async def test_refresh_keeps_previous_snapshots_when_everything_fails(mock_redis, poller, monkeypatch):
    monkeypatch.setattr(settings, "FETCH_RETRIES", 1)
    for url in ("https://news.google.com/rss?ceid=IN:en", "https://news.google.com/rss?ceid=GB:en",
                "https://asia.example/rss", "https://news.ycombinator.com/rss"):
        respx.get(url).mock(return_value=httpx.Response(500))
    await mock_redis.set(snapshot_key(ALL_VIEW), "previous")

    assert await refresh(mock_redis, poller) == 0

    assert await mock_redis.get(snapshot_key(ALL_VIEW)) == "previous"


@respx.mock
async def test_moved_feed_is_followed(mock_redis, poller):
    """A feed that redirects (moved, http->https) is still fetched."""
    mock_feeds()
    respx.get("https://asia.example/rss").mock(return_value=httpx.Response(301, headers={"Location": "https://asia.example/new-rss"}))
    respx.get("https://asia.example/new-rss").mock(return_value=httpx.Response(200, content=outlet_feed("asia.example", 30)))
    await refresh(mock_redis, poller)
    assert abs(sum("asia.example" in h["link"] for h in (await read_view(mock_redis, "asia"))["data"]) - 16 * 0.4) <= 1


# --- Topic views ---

TOPIC_CATALOG = Catalog(
    topics=[{"id": "sport", "label": "Sport"}, {"id": "uplifting", "label": "Uplifting", "feed_only": True}],
    regions=[
        {"id": "asia", "label": "Asia", "sources": [
            {"name": "GN India", "url": "https://news.google.com/rss?ceid=IN:en", "weight": 1.0, "label": "other"},
        ], "topic_sources": [
            {"name": "India Sports", "url": "https://sports.example/rss", "label": "sport"},
        ]},
        {"id": "europe", "label": "Europe", "sources": [
            {"name": "GN UK", "url": "https://news.google.com/rss?ceid=GB:en", "weight": 1.0},
        ]},
    ],
    extras=[{"name": "Hacker News", "url": "https://news.ycombinator.com/rss", "weight": 0.2}],
)
TAGGER = f"{settings.TAGGER_URL}/tag"


def fake_tagger(request):
    """Tags every third story (by its number) as sport, every fifth as uplifting."""
    items = json.loads(request.content)["items"]
    number = lambda item: int(item["title"].split()[-1])
    return httpx.Response(200, json={
        "tags": [["sport"] * (number(item) % 3 == 0) + ["uplifting"] * (number(item) % 5 == 0) for item in items],
        "model": "test",
    })


@pytest.fixture
def topic_catalog(monkeypatch):
    monkeypatch.setattr(catalog_module, "_catalog", TOPIC_CATALOG)
    mock_feeds()
    respx.get("https://sports.example/rss").mock(return_value=httpx.Response(200, content=outlet_feed("sports.example", 5)))


@pytest.fixture
def strict_sport(topic_catalog, monkeypatch):
    """The topic catalog, with sport leaving out the items of its feeds that the model scores under 0.05."""
    sport, uplifting = TOPIC_CATALOG.topics
    monkeypatch.setattr(catalog_module, "_catalog", TOPIC_CATALOG.model_copy(
        update={"topics": [sport.model_copy(update={"min_feed_score": 0.05}), uplifting]}))


@respx.mock
async def test_topic_views_are_published(mock_redis, poller, topic_catalog):
    tagger = respx.post(TAGGER).mock(side_effect=fake_tagger)
    assert await refresh(mock_redis, poller) == 3

    asia_sport = await read_view(mock_redis, "asia:sport")
    assert asia_sport["region"] == "asia" and asia_sport["topic"] == "sport"
    titles = [h["title"] for h in asia_sport["data"]]
    # Every item of its sports feed (its label is a tag) first, then the region's news the model tagged sport
    assert titles[:5] == [f"sports.example story {i}" for i in range(5)]
    assert all(int(t.split()[-1]) % 3 == 0 for t in titles if not t.startswith("sports.example"))
    assert any(t.startswith("in ") for t in titles) and any(t.startswith("hn.example") for t in titles)
    assert not any(t.startswith("gb ") for t in titles)
    assert not any("news.google.com" in h["link"] for h in asia_sport["data"])

    europe_sport = [h["title"] for h in (await read_view(mock_redis, "europe:sport"))["data"]]
    assert europe_sport and not any(t.startswith(("in ", "sports.example")) for t in europe_sport)
    all_sport = [h["title"] for h in (await read_view(mock_redis, "all:sport"))["data"]]
    assert any(t.startswith("in ") for t in all_sport) and any(t.startswith("gb ") for t in all_sport)
    # Uplifting comes only from feeds about it (none here), never from model tags; the
    # view is still published (empty) so the page can say so
    assert (await read_view(mock_redis, "asia:uplifting"))["data"] == []

    # Topic feeds only feed topic views
    assert not any(h["title"].startswith("sports.example") for h in (await read_view(mock_redis, "asia"))["data"])
    # The tagger gets every headline once, with its feed's label as training data
    sent = {item["title"]: item["label"] for item in json.loads(tagger.calls.last.request.content)["items"]}
    assert sent["in story 0"] == "other" and sent["sports.example story 0"] == "sport" and sent["gb story 0"] is None


@respx.mock
async def test_tagger_down_keeps_previous_topic_views(mock_redis, poller, topic_catalog):
    respx.post(TAGGER).mock(return_value=httpx.Response(503))
    await mock_redis.set(snapshot_key("asia:sport"), "previous")

    assert await refresh(mock_redis, poller) == 3  # The main views are unaffected

    assert await mock_redis.get(snapshot_key("asia:sport")) == "previous"
    assert (await read_view(mock_redis, "asia"))["data"]


@respx.mock
async def test_topic_feeds_are_fetched_every_interval(mock_redis, poller, topic_catalog):
    respx.post(TAGGER).mock(side_effect=fake_tagger)
    async with httpx.AsyncClient() as http_client:
        await refresh_once(http_client, mock_redis, poller)
        await refresh_once(http_client, mock_redis, poller)
    assert sum(call.request.url.host == "sports.example" for call in respx.calls) == 1
    # The second cycle still shows the topic feed's items
    assert any(h["title"].startswith("sports.example") for h in (await read_view(mock_redis, "asia:sport"))["data"])


@respx.mock
async def test_the_archives_stories_follow_the_tagger(mock_redis, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ARCHIVE_PATH", str(tmp_path / "archive.db"))
    monkeypatch.setattr("refresher.STORY_SYNC_BATCH", 2)
    items = [{"title": f"Story {n}", "link": f"https://pub.example/{n}", "source": "Pub", "published": 1.0} for n in range(3)]
    archive.archive({"all": items[:1] + [{**items[1], "story": 5}] + items[2:]}, 1000.0)
    moves = respx.get(f"{settings.TAGGER_URL}/story-moves").mock(side_effect=[
        httpx.Response(200, json={"moves": [{"id": 1, "old": 5, "new": 4, "title": None}]}),
        httpx.Response(200, json={"moves": []})])
    asked = []

    def stories(request):
        titles = [i["title"] for i in json.loads(request.content)["items"]]
        asked.append(titles)
        return httpx.Response(200, json={"stories": [int(t.split()[1]) + 10 for t in titles]})
    respx.post(f"{settings.TAGGER_URL}/stories").mock(side_effect=stories)

    async with httpx.AsyncClient() as client:
        await sync_stories(client)

    assert [call.request.url.params["after"] for call in moves.calls] == ["0", "1"]
    assert asked == [["Story 0", "Story 2"]]  # Only those without a story, in batches
    db = sqlite3.connect(settings.ARCHIVE_PATH)
    assert dict(db.execute("SELECT title, story FROM items")) == {"Story 0": 10, "Story 1": 4, "Story 2": 12}


@respx.mock
async def test_a_tagger_failure_leaves_the_archives_stories_for_the_next_cycle(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ARCHIVE_PATH", str(tmp_path / "archive.db"))
    archive.archive({"all": [{"title": "Story", "link": "https://pub.example/1", "source": "Pub", "published": 1.0}]}, 1000.0)
    respx.get(f"{settings.TAGGER_URL}/story-moves").mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as client:
        await sync_stories(client)  # Doesn't raise
    assert len(archive.without_story(10)) == 1


async def test_the_refresher_writes_the_search_index_for_the_app(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ARCHIVE_PATH", str(tmp_path / "archive.db"))
    archive.archive({"all": [{"title": "Bridge plan approved", "link": "https://pub.example/1", "source": "Pub", "published": 1.0}]}, 1000.0)
    await write_search_index()
    assert archive_index.Index(archive_index.index_path()).prefix("bri")[1] == archive_index.BitMap([1])
    monkeypatch.setattr(settings, "ARCHIVE_PATH", str(tmp_path / "missing" / "archive.db"))
    await write_search_index()  # A failure is logged, not raised


def embed_up():
    """The tagger's /embed (for the stats stream): a fingerprint for every headline."""
    respx.post(f"{settings.TAGGER_URL}/embed").mock(side_effect=lambda request: httpx.Response(
        200, json={"vectors": ["AAAA"] * len(json.loads(request.content)["items"])}))


@respx.mock
async def test_every_fetched_item_reaches_the_stats_stream(mock_redis, poller, monkeypatch):
    monkeypatch.setattr(settings, "MAX_HEADLINES", 5)  # Far fewer than the feeds carry
    stats_stream._sent.clear()
    mock_feeds()
    embed_up()
    await refresh(mock_redis, poller)
    links = {fields["link"] for _, fields in await mock_redis.xrange(stats_stream.STREAM)}
    assert len(links) > len((await read_view(mock_redis, ALL_VIEW))["data"]) == 5


@respx.mock
async def test_a_stream_failure_leaves_the_refresh_alone(mock_redis, poller, monkeypatch):
    def broken():
        raise RuntimeError("no catalog")
    monkeypatch.setattr(stats_stream, "get_catalog", broken)
    mock_feeds()
    assert await refresh(mock_redis, poller) == 3


async def raising_topic_views(*args):
    raise RuntimeError("no tagger")


@respx.mock
async def test_topic_feeds_reach_the_stream_when_the_topic_phase_fails(mock_redis, poller, monkeypatch):
    stats_stream._sent.clear()
    monkeypatch.setattr(refresher, "get_topic_views", raising_topic_views)
    poller.topic_feeds["results"] = {"Feed": [{"title": "T", "link": "https://t.example/1", "source": "S", "published": 1.0}]}
    embed_up()
    async with httpx.AsyncClient() as http_client:
        assert await refresher.refresh_topics(http_client, mock_redis, poller, {}) == 0
    assert await mock_redis.xlen(stats_stream.STREAM) == 1


async def test_the_search_index_is_rebuilt_only_every_so_often(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ARCHIVE_PATH", str(tmp_path / "archive.db"))
    monkeypatch.setattr(refresher, "_index_written", float("-inf"))
    archive.archive({"all": [{"title": "Bridge plan approved", "link": "https://pub.example/1", "source": "Pub", "published": 1.0}]}, 1000.0)
    await write_search_index()
    first = os.stat(archive_index.index_path()).st_ino
    await write_search_index()  # The next cycle: too soon
    assert os.stat(archive_index.index_path()).st_ino == first
    monkeypatch.setattr(settings, "ARCHIVE_INDEX_SECONDS", 0)
    await write_search_index()
    assert os.stat(archive_index.index_path()).st_ino != first


def test_an_extra_kept_to_some_tabs_is_only_on_those_and_the_news_fills_the_others(monkeypatch):
    from services.views.main_views import build_views
    monkeypatch.setattr(settings, "MAX_HEADLINES", 20)
    monkeypatch.setattr(settings, "STORY_GROUP_HEADROOM", 0)
    hn = CATALOG.extras[0].model_copy(update={"views": [ALL_VIEW]})
    limited = CATALOG.model_copy(update={"extras": [hn]})
    now = time.time()
    def items(prefix, count):
        return [{"title": f"{prefix} story {i}", "link": f"https://{prefix}.example/{i}", "source": prefix,
                 "published": now - i * 60} for i in range(count)]
    results = {"GN India": items("in", 30), "Outlet Asia": items("asia", 30), "GN UK": items("gb", 30),
               "Hacker News": items("hn", 30)}
    views = build_views(limited, results, now)
    hn_in = lambda view: sum(h["source"] == "hn" for h in views[view])
    assert hn_in(ALL_VIEW) == 4  # Its 0.2 of 20
    assert hn_in("asia") == 0 and hn_in("europe") == 0
    assert len(views["asia"]) == 20  # The region's news takes its place


def test_an_extra_can_only_be_kept_to_tabs_that_exist():
    from catalog import check_catalog
    hn = CATALOG.extras[0].model_copy(update={"views": ["mars"]})
    with pytest.raises(ValueError, match="mars"):
        check_catalog(CATALOG.model_copy(update={"extras": [hn]}))


def test_cycles_start_an_interval_apart_however_long_one_takes(monkeypatch):
    monkeypatch.setattr(settings, "REFRESH_INTERVAL_SECONDS", 300)
    assert until_next_cycle(started=1000.0, now=1025.0) == 275.0
    assert until_next_cycle(started=1000.0, now=1400.0) == 1.0  # A cycle that overran: the next one at once


async def test_a_restart_waits_for_the_cycle_the_last_run_had_due(mock_redis, monkeypatch):
    monkeypatch.setattr(settings, "REFRESH_INTERVAL_SECONDS", 300)
    assert await refresher.until_first_cycle(mock_redis) == 0  # Never run before: at once
    await refresher.note_cycle_start(mock_redis, now=1000.0)
    assert await refresher.until_first_cycle(mock_redis, now=1120.0) == 180.0  # Restarted 2 min into a cycle
    assert await refresher.until_first_cycle(mock_redis, now=1400.0) == 0  # Its next cycle is past due


async def test_a_restart_without_redis_starts_at_once(monkeypatch):
    class Down:
        async def get(self, key):
            raise refresher.aioredis.RedisError("down")
    assert await refresher.until_first_cycle(Down()) == 0


@respx.mock
async def test_topic_tabs_keep_at_most_a_few_headlines_about_one_subject(mock_redis, poller, topic_catalog, monkeypatch):
    monkeypatch.setattr(settings, "SUBJECT_CAP", 2)
    respx.post(TAGGER).mock(side_effect=fake_tagger)
    for i in range(4):  # Named by the summarizer, from an earlier cycle's tabs
        await mock_redis.set(subject_key(f"https://sports.example/a/{i}"), json.dumps({"subject": "Big Match"}))
    await refresh(mock_redis, poller)
    titles = [h["title"] for h in (await read_view(mock_redis, "asia:sport"))["data"]]
    assert titles[:3] == ["sports.example story 0", "sports.example story 1", "sports.example story 4"]
    assert "sports.example story 2" not in titles and "sports.example story 3" not in titles


@respx.mock
async def test_a_tabs_top_headlines_give_their_places_up_after_a_while(mock_redis, poller, monkeypatch):
    mock_feeds()
    async with httpx.AsyncClient() as http_client:
        await refresh_once(http_client, mock_redis, poller)
        first = [h["link"] for h in (await read_view(mock_redis, "asia"))["data"]]
        later = time.time() + settings.TOP_DWELL_SECONDS
        monkeypatch.setattr(refresher, "time", SimpleNamespace(time=lambda: later))  # The refresher's clock only
        await refresh_once(http_client, mock_redis, poller)
    second = [h["link"] for h in (await read_view(mock_redis, "asia"))["data"]]
    top = settings.TOP_POSITIONS
    assert len(second) == len(first) and not set(first[:top]) & set(second[:top])


@respx.mock
async def test_a_topic_can_leave_out_what_its_feeds_misfiled(mock_redis, poller, strict_sport, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "ARCHIVE_PATH", str(tmp_path / "archive.db"))

    def tagger(request):  # The model doesn't think sports.example story 1 is sport at all
        response = fake_tagger(request)
        items = json.loads(request.content)["items"]
        scores = [(0.01 if i["title"] == "sports.example story 1" else 0.7) if i["label"] == "sport" else None for i in items]
        return httpx.Response(200, json=response.json() | {"scores": scores})
    respx.post(TAGGER).mock(side_effect=tagger)
    await refresh(mock_redis, poller)
    titles = [h["title"] for h in (await read_view(mock_redis, "asia:sport"))["data"]]
    assert "sports.example story 0" in titles and "sports.example story 1" not in titles
    with sqlite3.connect(tmp_path / "archive.db") as db:  # Still searchable
        assert db.execute("SELECT COUNT(*) FROM items WHERE title = 'sports.example story 1'").fetchone()[0] == 1


@respx.mock
async def test_a_tagger_without_scores_leaves_every_feed_item_in(mock_redis, poller, strict_sport):
    respx.post(TAGGER).mock(side_effect=fake_tagger)
    await refresh(mock_redis, poller)
    titles = [h["title"] for h in (await read_view(mock_redis, "asia:sport"))["data"]]
    assert titles[:5] == [f"sports.example story {i}" for i in range(5)]


@respx.mock
async def test_the_llm_can_bring_a_flagged_feed_item_back(mock_redis, poller, strict_sport):
    # The tagger's model can miss what a section rightly filed (an AI cybercrime story in Technology);
    # the summarizer's LLM is asked about flagged items, and a yes brings them back
    flagged = {"sports.example story 1", "sports.example story 2", "sports.example story 4"}  # Not 3: the model tags it sport

    def tagger(request):
        items = json.loads(request.content)["items"]
        scores = [(0.01 if i["title"] in flagged else 0.7) if i["label"] == "sport" else None for i in items]
        return httpx.Response(200, json=fake_tagger(request).json() | {"scores": scores})
    respx.post(TAGGER).mock(side_effect=tagger)
    await mock_redis.set(topic_check_key("sport", "sports.example story 1"), json.dumps({"yes": True}))
    await mock_redis.set(topic_check_key("sport", "sports.example story 2"), json.dumps({"yes": False}))
    await refresh(mock_redis, poller)
    titles = [h["title"] for h in (await read_view(mock_redis, "asia:sport"))["data"]]
    assert "sports.example story 1" in titles  # The LLM said yes
    assert "sports.example story 2" not in titles and "sports.example story 4" not in titles  # No; not judged yet
    pending = json.loads(await mock_redis.get(topic_checks_pending_key()))
    assert [(p["topic"], p["title"]) for p in pending] == [("sport", "sports.example story 4")]


@respx.mock
async def test_extras_are_spread_over_a_tab_s_first_headlines(mock_redis, poller, monkeypatch):
    """However heavy the extras, news leads each tab and no two extras are side by side in its first headlines."""
    monkeypatch.setattr(catalog_module, "_catalog", Catalog(
        regions=CATALOG.model_dump()["regions"],
        extras=[{"name": "GN Science", "url": "https://news.google.com/rss?topic=science", "weight": 0.4},
                {"name": "Hacker News", "url": "https://news.ycombinator.com/rss", "weight": 0.3}]))
    monkeypatch.setattr(settings, "MAX_HEADLINES", 60)  # Enough news in each tab for its first headlines
    mock_feeds()
    respx.get("https://news.google.com/rss?topic=science").mock(return_value=httpx.Response(200, content=google_feed("sci", 30)))

    await refresh(mock_redis, poller)

    for view in ("all", "asia", "europe"):
        top = (await read_view(mock_redis, view))["data"][:settings.TOP_POSITIONS]
        extra = [h["link"].startswith(("https://publisher.example/sci-", "https://hn.example/")) for h in top]
        assert not extra[0]
        assert not any(a and b for a, b in zip(extra, extra[1:]))
        assert sum(extra) == settings.TOP_MAX_EXTRAS
