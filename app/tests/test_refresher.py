"""
Tests for the background refresher: per-region views and the "all" view,
link resolution, publishing, and keeping previous snapshots when a refresh
comes back empty.
"""

import json
import time

import httpx
import pytest
import respx
from prometheus_client import REGISTRY

import services.feed_service as feed_service
from config import ALL_VIEW, Catalog, settings, snapshot_key
from refresher import refresh_once

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
    monkeypatch.setattr(feed_service, "CATALOG", CATALOG)
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
async def test_refresh_publishes_every_view(mock_redis):
    mock_feeds()
    successes_before = sample("headlines_refreshes_total", {"result": "success"})

    async with httpx.AsyncClient() as http_client:
        assert await refresh_once(http_client, mock_redis) == 3  # all, asia, europe

    asia = await read_view(mock_redis, "asia")
    europe = await read_view(mock_redis, "europe")
    everything = await read_view(mock_redis, ALL_VIEW)
    assert asia["region"] == "asia" and europe["region"] == "europe" and everything["region"] == ALL_VIEW
    assert everything["expires_at"] == pytest.approx(everything["fetched_at"] + settings.REFRESH_INTERVAL_SECONDS)

    # Every view has the same mix: 80% news (that region's, or all regions equally), 20% HN
    asia_titles = [h["title"] for h in asia["data"]]
    assert len(asia_titles) == 20
    assert sum(t.startswith("hn.example") for t in asia_titles) == 4
    assert abs(sum(t.startswith("in ") for t in asia_titles) - 16 * 0.6) <= 1  # Region weights within the news share
    assert sum(t.startswith(("in ", "asia.example")) for t in asia_titles) == 16
    assert sum(t.startswith("gb ") for t in (h["title"] for h in europe["data"])) == 16

    all_titles = [h["title"] for h in everything["data"]]
    assert len(all_titles) == 20
    assert sum(t.startswith("hn.example") for t in all_titles) == 4
    assert sum(t.startswith(("in ", "asia.example")) for t in all_titles) == 8
    assert sum(t.startswith("gb ") for t in all_titles) == 8

    # News leads every view (it has the largest weight)
    for view in (asia, europe, everything):
        assert not view["data"][0]["title"].startswith("hn.example")

    # Google links resolved (in every view), direct links and HN discussion links untouched
    for view in (asia, europe, everything):
        assert not any("news.google.com" in h["link"] for h in view["data"])
    hn = [h for h in everything["data"] if h["title"].startswith("hn.example")]
    assert hn[0]["link"].startswith("https://hn.example/") and hn[0]["comments"].startswith("https://news.ycombinator.com/item")
    assert sample("headlines_refreshes_total", {"result": "success"}) == successes_before + 1


@respx.mock
async def test_single_outlet_feed_is_not_capped_in_its_region(mock_redis):
    """An outlet's own feed (all items from one publisher) fills its weight; the cap would allow only 3."""
    mock_feeds()
    async with httpx.AsyncClient() as http_client:
        await refresh_once(http_client, mock_redis)
    asia = await read_view(mock_redis, "asia")
    everything = await read_view(mock_redis, ALL_VIEW)
    assert abs(sum("asia.example" in h["link"] for h in asia["data"]) - 16 * 0.4) <= 1
    # In "all" it comes via the multi-publisher Asia mix, so the per-publisher cap applies
    assert sum("asia.example" in h["link"] for h in everything["data"]) == settings.MAX_PER_PUBLISHER


@respx.mock
async def test_empty_view_keeps_its_previous_snapshot(mock_redis, monkeypatch):
    monkeypatch.setattr(settings, "FETCH_RETRIES", 1)
    mock_feeds()
    respx.get("https://news.google.com/rss?ceid=GB:en").mock(return_value=httpx.Response(500))
    await mock_redis.set(snapshot_key("europe"), "previous europe")

    async with httpx.AsyncClient() as http_client:
        assert await refresh_once(http_client, mock_redis) == 2  # all, asia

    assert await mock_redis.get(snapshot_key("europe")) == "previous europe"
    assert (await read_view(mock_redis, "asia"))["data"]


@respx.mock
async def test_refresh_keeps_previous_snapshots_when_everything_fails(mock_redis, monkeypatch):
    monkeypatch.setattr(settings, "FETCH_RETRIES", 1)
    for url in ("https://news.google.com/rss?ceid=IN:en", "https://news.google.com/rss?ceid=GB:en",
                "https://asia.example/rss", "https://news.ycombinator.com/rss"):
        respx.get(url).mock(return_value=httpx.Response(500))
    await mock_redis.set(snapshot_key(ALL_VIEW), "previous")
    empty_before = sample("headlines_refreshes_total", {"result": "empty"})
    failures_before = sample("feed_fetch_failures_total", {"source": "GN India"})

    async with httpx.AsyncClient() as http_client:
        assert await refresh_once(http_client, mock_redis) == 0

    assert await mock_redis.get(snapshot_key(ALL_VIEW)) == "previous"
    assert sample("headlines_refreshes_total", {"result": "empty"}) == empty_before + 1
    assert sample("feed_fetch_failures_total", {"source": "GN India"}) == failures_before + 1


def test_shipped_catalog_is_valid():
    """sources.json loads, every region and the "all" view have sources, and every region has topic feeds."""
    from config import CATALOG as shipped
    assert shipped.view_ids[0] == ALL_VIEW and len(shipped.view_ids) == len(shipped.regions) + 1
    assert all(region.sources and region.topic_sources for region in shipped.regions)
    assert len(shipped.topic_view_ids) == len(shipped.view_ids) * len(shipped.topics)


def test_source_labels_must_be_topics(tmp_path):
    from config import load_catalog
    path = tmp_path / "sources.json"
    path.write_text(json.dumps({
        "topics": [{"id": "sport", "label": "Sport"}],
        "regions": [{"id": "asia", "label": "Asia", "sources": [{"name": "A", "url": "https://a", "weight": 1.0}],
                     "topic_sources": [{"name": "B", "url": "https://b", "label": "gossip"}]}],
        "extras": [],
    }))
    with pytest.raises(ValueError, match="gossip"):
        load_catalog(path)


@respx.mock
async def test_moved_feed_is_followed(mock_redis):
    """A feed that redirects (moved, http->https) is still fetched."""
    mock_feeds()
    respx.get("https://asia.example/rss").mock(return_value=httpx.Response(301, headers={"Location": "https://asia.example/new-rss"}))
    respx.get("https://asia.example/new-rss").mock(return_value=httpx.Response(200, content=outlet_feed("asia.example", 30)))
    async with httpx.AsyncClient() as http_client:
        await refresh_once(http_client, mock_redis)
    assert abs(sum("asia.example" in h["link"] for h in (await read_view(mock_redis, "asia"))["data"]) - 16 * 0.4) <= 1


# --- Topic views ---

TOPIC_CATALOG = Catalog(
    topics=[{"id": "sport", "label": "Sport"}, {"id": "uplifting", "label": "Uplifting"}],
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
    """Tags every third story (by its number) as sport."""
    items = json.loads(request.content)["items"]
    return httpx.Response(200, json={
        "tags": [["sport"] if int(item["title"].split()[-1]) % 3 == 0 else [] for item in items],
        "model": "test",
    })


@pytest.fixture
def topic_catalog(monkeypatch):
    monkeypatch.setattr(feed_service, "CATALOG", TOPIC_CATALOG)
    monkeypatch.setattr(feed_service, "_topic_feeds", {"at": float("-inf"), "results": {}})
    mock_feeds()
    respx.get("https://sports.example/rss").mock(return_value=httpx.Response(200, content=outlet_feed("sports.example", 5)))


@respx.mock
async def test_topic_views_are_published(mock_redis, topic_catalog):
    tagger = respx.post(TAGGER).mock(side_effect=fake_tagger)
    async with httpx.AsyncClient() as http_client:
        assert await refresh_once(http_client, mock_redis) == 3

    asia_sport = await read_view(mock_redis, "asia:sport")
    assert asia_sport["region"] == "asia" and asia_sport["topic"] == "sport"
    titles = [h["title"] for h in asia_sport["data"]]
    # The region's news tagged sport, plus every item of its sports feed (its label is a tag), which leads
    assert {t for t in titles if t.startswith("sports.example")} == {f"sports.example story {i}" for i in range(5)}
    assert titles[0] == "sports.example story 0"
    assert all(int(t.split()[-1]) % 3 == 0 for t in titles if not t.startswith("sports.example"))
    assert any(t.startswith("in ") for t in titles) and any(t.startswith("hn.example") for t in titles)
    assert not any(t.startswith("gb ") for t in titles)
    assert not any("news.google.com" in h["link"] for h in asia_sport["data"])

    europe_sport = [h["title"] for h in (await read_view(mock_redis, "europe:sport"))["data"]]
    assert europe_sport and not any(t.startswith(("in ", "sports.example")) for t in europe_sport)
    all_sport = [h["title"] for h in (await read_view(mock_redis, "all:sport"))["data"]]
    assert any(t.startswith("in ") for t in all_sport) and any(t.startswith("gb ") for t in all_sport)
    # Nothing is uplifting, but the view is published (empty) so the page can say so
    assert (await read_view(mock_redis, "asia:uplifting"))["data"] == []

    # Topic feeds only feed topic views
    assert not any(h["title"].startswith("sports.example") for h in (await read_view(mock_redis, "asia"))["data"])
    # The tagger gets every headline once, with its feed's label as training data
    sent = {item["title"]: item["label"] for item in json.loads(tagger.calls.last.request.content)["items"]}
    assert sent["in story 0"] == "other" and sent["sports.example story 0"] == "sport" and sent["gb story 0"] is None


@respx.mock
async def test_tagger_down_keeps_previous_topic_views(mock_redis, topic_catalog):
    respx.post(TAGGER).mock(return_value=httpx.Response(503))
    await mock_redis.set(snapshot_key("asia:sport"), "previous")
    failures_before = sample("headlines_topic_refreshes_total", {"result": "error"})

    async with httpx.AsyncClient() as http_client:
        assert await refresh_once(http_client, mock_redis) == 3  # The main views are unaffected

    assert await mock_redis.get(snapshot_key("asia:sport")) == "previous"
    assert (await read_view(mock_redis, "asia"))["data"]
    assert sample("headlines_topic_refreshes_total", {"result": "error"}) == failures_before + 1


@respx.mock
async def test_topic_feeds_are_fetched_every_interval(mock_redis, topic_catalog):
    respx.post(TAGGER).mock(side_effect=fake_tagger)
    async with httpx.AsyncClient() as http_client:
        await refresh_once(http_client, mock_redis)
        await refresh_once(http_client, mock_redis)
    assert sum(call.request.url.host == "sports.example" for call in respx.calls) == 1
    # The second cycle still shows the topic feed's items
    assert any(h["title"].startswith("sports.example") for h in (await read_view(mock_redis, "asia:sport"))["data"])
