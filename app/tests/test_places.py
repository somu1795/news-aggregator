"""Tests for finding which regions a headline is about."""

import time

import pytest

from config import Catalog
from services.places import regions_in


@pytest.mark.parametrize("title, regions", [
    ("Governors allied with Flávio Bolsonaro set to lead majority of Brazilian Amazon states", {"americas"}),
    ("Inside the ‘unique’ tail of Iran’s spider-tailed horned viper", {"asia"}),
    ("As sand becomes more valuable, Sierra Leone’s Mahera Coast grows more vulnerable", {"africa"}),
    ("New Zealand builds an arsenal of tools to battle a ‘wily’ invasive seaweed", {"oceania"}),
    ("These California Boat Dwellers Fear the Worst: Life on Land", {"americas"}),
    ("Koalas return to New South Wales forests", {"oceania"}),  # Not Wales
    ("Papua New Guinea logging probe", {"oceania"}),  # Not Guinea
    ("Flights from São Paulo resume", {"americas"}),  # A name that wraps onto the next line in the list
    ("US and China agree climate pact", {"americas", "asia"}),
    ("U.S. EPA rolls back rule", {"americas"}),
    ("Too much penguin poop in Antarctica is accelerating snow melt", set()),
    ("Let us protect the oceans", set()),  # "us", not the US
    ("Georgia peaches wilt in heat", set()),  # Left out: a US state and a country
    ("Turkey vultures return to the valley", set()),  # Left out: a bird
    ("Amazon warehouses go solar", set()),  # Left out: the company
])
def test_regions_in(title, regions):
    assert regions_in(title) == regions


def test_shared_stories_go_to_the_region_they_name():
    from services.feed_service import build_topic_views
    catalog = Catalog(
        regions=[{"id": r, "label": r, "sources": [{"name": f"{r} news", "url": f"https://{r}.example/rss", "weight": 1.0}]}
                 for r in ("africa", "asia")],
        extras=[],
        topics=[{"id": "nature", "label": "Nature"}],
        topic_extras=[{"name": "Mongabay", "url": "https://mongabay.example/rss", "label": "nature"}],
    )
    now = time.time()

    def story(title):
        return {"title": title, "link": f"https://mongabay.example/{title[:20]}", "source": "Mongabay", "published": now}

    results = {"Mongabay": [story("Sierra Leone’s coast erodes"), story("Iran’s horned viper"), story("Penguins in Antarctica")]}
    tags = {h["title"]: {"nature"} for h in results["Mongabay"]}
    views = build_topic_views(catalog, results, tags, now)
    assert [h["title"] for h in views["africa:nature"]] == ["Sierra Leone’s coast erodes", "Penguins in Antarctica"]
    assert [h["title"] for h in views["asia:nature"]] == ["Iran’s horned viper", "Penguins in Antarctica"]
    assert len(views["all:nature"]) == 3  # "All" keeps everything


def test_feed_text_names_the_place_when_the_headline_doesnt():
    from services.places import item_regions
    item = {"title": "Ministers preparing to slash heat pump subsidies",
            "feed_text": "The UK government is expected to cut grants for heat pumps in England to fund defence."}
    assert item_regions(item) == {"europe"}
    assert item_regions({"title": "Iran’s horned viper", "feed_text": "Scientists in Brazil…"}) == {"asia"}  # Headline first
    assert item_regions({"title": "The story of birds starts with their feathers"}) == set()
