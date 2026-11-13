"""Tests for building the topic tabs from fetched feeds and their tags (services/views/topic_views.py)."""

from catalog import Catalog
from services.views.topic_views import build_topic_views

NOW = 1_800_000_000.0


def story(title, host, days_old=0):
    return {"title": title, "link": f"https://{host}/{title.replace(' ', '-')}", "source": host, "published": NOW - days_old * 86400}


def test_region_topic_views_put_their_own_stories_first():
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

    results = {
        "asia tech": [story("Global chip launch", "techcrunch.com"), story("Singapore startup raises funds", "sg.example")],
        "europe tech": [story("Global chip launch", "techcrunch.com"), story("UK telecom outage", "uk.example")],
        "Shared tech": [story("Open source release", "shared.example")],
    }
    tags = {h["title"]: {"tech"} for items in results.values() for h in items}
    views = build_topic_views(catalog, results, tags, NOW)

    asia = [h["title"] for h in views["asia:tech"]]
    # The region's own stories first, then the shared feed's, then the story both regions have
    assert asia == ["Singapore startup raises funds", "Open source release", "Global chip launch"]
    assert [h["title"] for h in views["europe:tech"]] == ["UK telecom outage", "Open source release", "Global chip launch"]
    assert "Global chip launch" in [h["title"] for h in views["all:tech"]]


def test_a_regions_own_feed_takes_turns_with_shared_stories_about_the_region():
    # The Americas have one nature feed of their own (Inside Climate News): it filled the first 7
    # places before Mongabay's or the Guardian's stories about the Americas
    catalog = Catalog(
        regions=[{"id": "asia", "label": "Asia", "sources": [{"name": "asia news", "url": "https://asia.example/rss", "weight": 1.0}],
                  "topic_sources": [{"name": "asia nature", "url": "https://asia.example/nature", "label": "nature"}],
                  "places": ["Singapore"]}],
        extras=[],
        topics=[{"id": "nature", "label": "Nature", "feed_only": True}],
        topic_extras=[{"name": "Shared nature", "url": "https://shared.example/rss", "label": "nature"}],
    )

    results = {
        "asia nature": [story(f"Own story {i}", "own.example") for i in range(3)],
        "Shared nature": [story(f"Singapore wildlife story {i}", "shared.example") for i in range(2)],
    }
    tags = {h["title"]: {"nature"} for items in results.values() for h in items}
    views = build_topic_views(catalog, results, tags, NOW)
    assert [h["title"] for h in views["asia:nature"]] == [
        "Own story 0", "Singapore wildlife story 0", "Own story 1", "Singapore wildlife story 1", "Own story 2"]


def test_a_topic_can_keep_older_stories():
    catalog = Catalog(
        regions=[{"id": "asia", "label": "Asia", "sources": [{"name": "news", "url": "https://n.example/rss", "weight": 1.0}]}],
        extras=[],
        topics=[{"id": "uplifting", "label": "Uplifting", "max_age_hours": 168, "feed_only": True}, {"id": "tech", "label": "Tech"}],
        topic_extras=[{"name": "Good news", "url": "https://g.example/rss", "label": "uplifting"},
                      {"name": "Tech news", "url": "https://t.example/rss", "label": "tech"}],
    )

    results = {"Good news": [story("good-today", "x.example", days_old=0), story("good-5-days", "x.example", days_old=5), story("good-9-days", "x.example", days_old=9)],
               "Tech news": [story("tech-today", "x.example", days_old=0), story("tech-5-days", "x.example", days_old=5)]}
    tags = {"good-today": {"uplifting"}, "good-5-days": {"uplifting"}, "good-9-days": {"uplifting"},
            "tech-today": {"tech"}, "tech-5-days": {"tech"}}
    views = build_topic_views(catalog, results, tags, NOW)
    assert [h["title"] for h in views["asia:uplifting"]] == ["good-today", "good-5-days"]  # 7 days
    assert [h["title"] for h in views["asia:tech"]] == ["tech-today"]  # The default 48 hours
