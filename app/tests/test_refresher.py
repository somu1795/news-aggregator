"""
Tests for the background refresher: per-region views and the "all" view,
link resolution, publishing, and keeping previous snapshots when a refresh
comes back empty.
"""

import json
import sqlite3
import time

import httpx
import pytest
import respx
from prometheus_client import REGISTRY

import catalog as catalog_module
from catalog import ALL_VIEW, Catalog
from config import settings
from keys import snapshot_key, subject_key
import refresher
from refresher import refresh_once
from services import stats_stream

pytestmark = pytest.mark.asyncio

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


def sample(name, labels=None):
    return REGISTRY.get_sample_value(name, labels or {}) or 0


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


@respx.mock
async def test_refresh_publishes_every_view(mock_redis, poller):
    mock_feeds()
    successes_before = sample("headlines_refreshes_total", {"result": "success"})

    async with httpx.AsyncClient() as http_client:
        assert await refresh_once(http_client, mock_redis, poller) == 3  # all, asia, europe

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
    assert sample("headlines_refreshes_total", {"result": "success"}) == successes_before + 1


@respx.mock
async def test_single_outlet_feed_is_not_capped_in_its_region(mock_redis, poller):
    """An outlet's own feed (all items from one publisher) fills its weight; the cap would allow only 3."""
    mock_feeds()
    async with httpx.AsyncClient() as http_client:
        await refresh_once(http_client, mock_redis, poller)
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

    async with httpx.AsyncClient() as http_client:
        assert await refresh_once(http_client, mock_redis, poller) == 2  # all, asia

    assert await mock_redis.get(snapshot_key("europe")) == "previous europe"
    assert (await read_view(mock_redis, "asia"))["data"]


@respx.mock
async def test_refresh_keeps_previous_snapshots_when_everything_fails(mock_redis, poller, monkeypatch):
    monkeypatch.setattr(settings, "FETCH_RETRIES", 1)
    for url in ("https://news.google.com/rss?ceid=IN:en", "https://news.google.com/rss?ceid=GB:en",
                "https://asia.example/rss", "https://news.ycombinator.com/rss"):
        respx.get(url).mock(return_value=httpx.Response(500))
    await mock_redis.set(snapshot_key(ALL_VIEW), "previous")
    empty_before = sample("headlines_refreshes_total", {"result": "empty"})
    failures_before = sample("feed_fetch_failures_total", {"source": "GN India"})

    async with httpx.AsyncClient() as http_client:
        assert await refresh_once(http_client, mock_redis, poller) == 0

    assert await mock_redis.get(snapshot_key(ALL_VIEW)) == "previous"
    assert sample("headlines_refreshes_total", {"result": "empty"}) == empty_before + 1
    assert sample("feed_fetch_failures_total", {"source": "GN India"}) == failures_before + 1


def test_shipped_catalog_is_valid():
    """The config folder loads, every region and the "all" view have sources, and every region has topic feeds."""
    from pathlib import Path
    from catalog import load_catalog
    shipped = load_catalog(Path(settings.CONFIG_DIR))
    assert shipped.view_ids[0] == ALL_VIEW and len(shipped.view_ids) == len(shipped.regions) + 1
    assert all(region.sources and region.topic_sources for region in shipped.regions)
    assert len(shipped.topic_view_ids) == len(shipped.view_ids) * len(shipped.topics)


def test_source_labels_must_be_topics(tmp_path):
    from catalog import load_catalog
    (tmp_path / "topics.yaml").write_text(json.dumps({"topics": [{"id": "sport", "label": "Sport"}]}))
    (tmp_path / "regions.yaml").write_text(json.dumps({"regions": [{"id": "asia", "label": "Asia"}]}))
    (tmp_path / "sources.yaml").write_text(json.dumps({  # JSON is YAML too
        "regions": {"asia": {"sources": [{"name": "A", "url": "https://a", "weight": 1.0}],
                             "topic_sources": [{"name": "B", "url": "https://b", "label": "gossip"}]}},
        "extras": [],
    }))
    with pytest.raises(ValueError, match="gossip"):
        load_catalog(tmp_path)


@respx.mock
async def test_moved_feed_is_followed(mock_redis, poller):
    """A feed that redirects (moved, http->https) is still fetched."""
    mock_feeds()
    respx.get("https://asia.example/rss").mock(return_value=httpx.Response(301, headers={"Location": "https://asia.example/new-rss"}))
    respx.get("https://asia.example/new-rss").mock(return_value=httpx.Response(200, content=outlet_feed("asia.example", 30)))
    async with httpx.AsyncClient() as http_client:
        await refresh_once(http_client, mock_redis, poller)
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


@respx.mock
async def test_topic_views_are_published(mock_redis, poller, topic_catalog):
    tagger = respx.post(TAGGER).mock(side_effect=fake_tagger)
    async with httpx.AsyncClient() as http_client:
        assert await refresh_once(http_client, mock_redis, poller) == 3

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
    failures_before = sample("headlines_topic_refreshes_total", {"result": "error"})

    async with httpx.AsyncClient() as http_client:
        assert await refresh_once(http_client, mock_redis, poller) == 3  # The main views are unaffected

    assert await mock_redis.get(snapshot_key("asia:sport")) == "previous"
    assert (await read_view(mock_redis, "asia"))["data"]
    assert sample("headlines_topic_refreshes_total", {"result": "error"}) == failures_before + 1


@respx.mock
async def test_topic_feeds_are_fetched_every_interval(mock_redis, poller, topic_catalog):
    respx.post(TAGGER).mock(side_effect=fake_tagger)
    async with httpx.AsyncClient() as http_client:
        await refresh_once(http_client, mock_redis, poller)
        await refresh_once(http_client, mock_redis, poller)
    assert sum(call.request.url.host == "sports.example" for call in respx.calls) == 1
    # The second cycle still shows the topic feed's items
    assert any(h["title"].startswith("sports.example") for h in (await read_view(mock_redis, "asia:sport"))["data"])


def test_comments_link_to_the_article_itself_is_dropped():
    from services.feeds.parse import discussion_link
    article = "https://mexiconewsdaily.com/news/story/"
    assert discussion_link(article, article) is None
    assert discussion_link(article, article + "#comments") is None
    assert discussion_link(article, None) is None
    assert discussion_link("https://hn.example/a", "https://news.ycombinator.com/item?id=1") == "https://news.ycombinator.com/item?id=1"


def test_region_topic_views_put_their_own_stories_first():
    from services.views.topic_views import build_topic_views
    catalog = Catalog(
        regions=[
            {"id": r, "label": r, "sources": [{"name": f"{r} news", "url": f"https://{r}.example/rss", "weight": 1.0}],
             "topic_sources": [{"name": f"{r} tech", "url": f"https://{r}.example/tech", "label": "tech"}]}
            for r in ("asia", "europe")
        ],
        extras=[],
        topics=[{"id": "tech", "label": "Tech"}],
        topic_extras=[{"name": "Shared tech", "url": "https://shared.example/rss", "label": "tech"}],
    )
    now = time.time()

    def story(title, host):
        return {"title": title, "link": f"https://{host}/{title.replace(' ', '-')}", "source": host, "published": now}

    results = {
        "asia tech": [story("Global chip launch", "techcrunch.com"), story("Singapore startup raises funds", "sg.example")],
        "europe tech": [story("Global chip launch", "techcrunch.com"), story("UK telecom outage", "uk.example")],
        "Shared tech": [story("Open source release", "shared.example")],
    }
    tags = {h["title"]: {"tech"} for items in results.values() for h in items}
    views = build_topic_views(catalog, results, tags, now)

    asia = [h["title"] for h in views["asia:tech"]]
    # The region's own stories first, then the shared feed's, then the story both regions have
    assert asia == ["Singapore startup raises funds", "Open source release", "Global chip launch"]
    assert [h["title"] for h in views["europe:tech"]] == ["UK telecom outage", "Open source release", "Global chip launch"]
    assert "Global chip launch" in [h["title"] for h in views["all:tech"]]


def test_a_topic_can_keep_older_stories():
    from services.views.topic_views import build_topic_views
    catalog = Catalog(
        regions=[{"id": "asia", "label": "Asia", "sources": [{"name": "news", "url": "https://n.example/rss", "weight": 1.0}]}],
        extras=[],
        topics=[{"id": "uplifting", "label": "Uplifting", "max_age_hours": 168, "feed_only": True}, {"id": "tech", "label": "Tech"}],
        topic_extras=[{"name": "Good news", "url": "https://g.example/rss", "label": "uplifting"},
                      {"name": "Tech news", "url": "https://t.example/rss", "label": "tech"}],
    )
    now = time.time()

    def story(title, days_old):
        return {"title": title, "link": f"https://x.example/{title}", "source": "X", "published": now - days_old * 86400}

    results = {"Good news": [story("good-today", 0), story("good-5-days", 5), story("good-9-days", 9)],
               "Tech news": [story("tech-today", 0), story("tech-5-days", 5)]}
    tags = {"good-today": {"uplifting"}, "good-5-days": {"uplifting"}, "good-9-days": {"uplifting"},
            "tech-today": {"tech"}, "tech-5-days": {"tech"}}
    views = build_topic_views(catalog, results, tags, now)
    assert [h["title"] for h in views["asia:uplifting"]] == ["good-today", "good-5-days"]  # 7 days
    assert [h["title"] for h in views["asia:tech"]] == ["tech-today"]  # The default 48 hours


def test_a_source_can_be_capped():
    from catalog import NewsSource
    from services.views.mixing import mix_headlines
    now = time.time()
    sources = [NewsSource(name="Busy", url="https://b.example/rss", weight=0.9, max_items=2),
               NewsSource(name="Quiet", url="https://q.example/rss", weight=0.1)]
    results = {name: [{"title": f"{name} {i}", "link": f"https://{name}.example/{i}", "source": name, "published": now}
                      for i in range(5)] for name in ("Busy", "Quiet")}
    titles = [h["title"] for h in mix_headlines(sources, results, now)]
    assert sum(t.startswith("Busy") for t in titles) == 2  # Its weight would have given it most of the list
    assert sum(t.startswith("Quiet") for t in titles) == 5  # The others fill its share


@respx.mock
async def test_the_archives_stories_follow_the_tagger(mock_redis, tmp_path, monkeypatch):
    from refresher import STORY_SYNC_BATCH, sync_stories
    from services import archive
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
    assert STORY_SYNC_BATCH == 2000


@respx.mock
async def test_a_tagger_failure_leaves_the_archives_stories_for_the_next_cycle(tmp_path, monkeypatch):
    from refresher import sync_stories
    from services import archive
    monkeypatch.setattr(settings, "ARCHIVE_PATH", str(tmp_path / "archive.db"))
    archive.archive({"all": [{"title": "Story", "link": "https://pub.example/1", "source": "Pub", "published": 1.0}]}, 1000.0)
    respx.get(f"{settings.TAGGER_URL}/story-moves").mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as client:
        await sync_stories(client)  # Doesn't raise
    assert len(archive.without_story(10)) == 1


async def test_the_refresher_writes_the_search_index_for_the_app(tmp_path, monkeypatch):
    from refresher import write_search_index
    from services import archive, archive_index
    monkeypatch.setattr(settings, "ARCHIVE_PATH", str(tmp_path / "archive.db"))
    archive.archive({"all": [{"title": "Bridge plan approved", "link": "https://pub.example/1", "source": "Pub", "published": 1.0}]}, 1000.0)
    await write_search_index()
    assert archive_index.Index(archive_index.index_path()).prefix("bri")[1] == archive_index.BitMap([1])
    monkeypatch.setattr(settings, "ARCHIVE_PATH", str(tmp_path / "missing" / "archive.db"))
    await write_search_index()  # A failure is logged, not raised


@respx.mock
async def test_every_fetched_item_reaches_the_stats_stream(mock_redis, poller, monkeypatch):
    monkeypatch.setattr(settings, "MAX_HEADLINES", 5)  # Far fewer than the feeds carry
    stats_stream._sent.clear()
    mock_feeds()
    async with httpx.AsyncClient() as http_client:
        await refresh_once(http_client, mock_redis, poller)
    links = {fields["link"] for _, fields in await mock_redis.xrange(stats_stream.STREAM)}
    assert len(links) > len(await read_view(mock_redis, ALL_VIEW))


@respx.mock
async def test_a_stream_failure_leaves_the_refresh_alone(mock_redis, poller, monkeypatch):
    def broken():
        raise RuntimeError("no catalog")
    monkeypatch.setattr(stats_stream, "get_catalog", broken)
    mock_feeds()
    async with httpx.AsyncClient() as http_client:
        assert await refresh_once(http_client, mock_redis, poller) == 3


async def test_topic_feeds_reach_the_stream_when_the_topic_phase_fails(mock_redis, poller, monkeypatch):
    stats_stream._sent.clear()
    monkeypatch.setattr(refresher, "get_topic_views", raising_topic_views)
    poller.topic_feeds["results"] = {"Feed": [{"title": "T", "link": "https://t.example/1", "source": "S", "published": 1.0}]}
    async with httpx.AsyncClient() as http_client:
        assert await refresher.refresh_topics(http_client, mock_redis, poller, {}) == 0
    assert await mock_redis.xlen(stats_stream.STREAM) == 1


async def raising_topic_views(*args):
    raise RuntimeError("no tagger")


async def test_the_search_index_is_rebuilt_only_every_so_often(tmp_path, monkeypatch):
    import os
    import refresher
    from services import archive, archive_index
    monkeypatch.setattr(settings, "ARCHIVE_PATH", str(tmp_path / "archive.db"))
    monkeypatch.setattr(refresher, "_index_written", float("-inf"))
    archive.archive({"all": [{"title": "Bridge plan approved", "link": "https://pub.example/1", "source": "Pub", "published": 1.0}]}, 1000.0)
    await refresher.write_search_index()
    first = os.stat(archive_index.index_path()).st_ino
    await refresher.write_search_index()  # The next cycle: too soon
    assert os.stat(archive_index.index_path()).st_ino == first
    monkeypatch.setattr(settings, "ARCHIVE_INDEX_SECONDS", 0)
    await refresher.write_search_index()
    assert os.stat(archive_index.index_path()).st_ino != first


def test_cycles_start_an_interval_apart_however_long_one_takes(monkeypatch):
    from refresher import until_next_cycle
    monkeypatch.setattr(settings, "REFRESH_INTERVAL_SECONDS", 300)
    assert until_next_cycle(started=1000.0, now=1025.0) == 275.0
    assert until_next_cycle(started=1000.0, now=1400.0) == 1.0  # A cycle that overran: the next one at once


@respx.mock
async def test_topic_tabs_keep_at_most_a_few_headlines_about_one_subject(mock_redis, poller, topic_catalog, monkeypatch):
    monkeypatch.setattr(settings, "SUBJECT_CAP", 2)
    respx.post(TAGGER).mock(side_effect=fake_tagger)
    for i in range(4):  # Named by the summarizer, from an earlier cycle's tabs
        await mock_redis.set(subject_key(f"https://sports.example/a/{i}"), json.dumps({"subject": "Big Match"}))
    async with httpx.AsyncClient() as http_client:
        await refresh_once(http_client, mock_redis, poller)
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
        monkeypatch.setattr(refresher.time, "time", lambda: later)
        await refresh_once(http_client, mock_redis, poller)
    second = [h["link"] for h in (await read_view(mock_redis, "asia"))["data"]]
    top = settings.TOP_POSITIONS
    assert len(second) == len(first) and not set(first[:top]) & set(second[:top])


@respx.mock
async def test_a_topic_can_leave_out_what_its_feeds_misfiled(mock_redis, poller, topic_catalog, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "ARCHIVE_PATH", str(tmp_path / "archive.db"))
    strict = TOPIC_CATALOG.model_copy(update={"topics": [
        TOPIC_CATALOG.topics[0].model_copy(update={"min_feed_score": 0.05}), TOPIC_CATALOG.topics[1]]})
    monkeypatch.setattr(catalog_module, "_catalog", strict)

    def tagger(request):  # The model doesn't think sports.example story 1 is sport at all
        response = fake_tagger(request)
        items = json.loads(request.content)["items"]
        scores = [(0.01 if i["title"] == "sports.example story 1" else 0.7) if i["label"] == "sport" else None for i in items]
        return httpx.Response(200, json=response.json() | {"scores": scores})
    respx.post(TAGGER).mock(side_effect=tagger)
    async with httpx.AsyncClient() as http_client:
        await refresh_once(http_client, mock_redis, poller)
    titles = [h["title"] for h in (await read_view(mock_redis, "asia:sport"))["data"]]
    assert "sports.example story 0" in titles and "sports.example story 1" not in titles
    with sqlite3.connect(tmp_path / "archive.db") as db:  # Still searchable
        assert db.execute("SELECT COUNT(*) FROM items WHERE title = 'sports.example story 1'").fetchone()[0] == 1


@respx.mock
async def test_a_tagger_without_scores_leaves_every_feed_item_in(mock_redis, poller, topic_catalog, monkeypatch):
    strict = TOPIC_CATALOG.model_copy(update={"topics": [
        TOPIC_CATALOG.topics[0].model_copy(update={"min_feed_score": 0.05}), TOPIC_CATALOG.topics[1]]})
    monkeypatch.setattr(catalog_module, "_catalog", strict)
    respx.post(TAGGER).mock(side_effect=fake_tagger)
    async with httpx.AsyncClient() as http_client:
        await refresh_once(http_client, mock_redis, poller)
    titles = [h["title"] for h in (await read_view(mock_redis, "asia:sport"))["data"]]
    assert titles[:5] == [f"sports.example story {i}" for i in range(5)]
