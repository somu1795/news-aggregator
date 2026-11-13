"""Tests for the site's config folder as a catalog (catalog.py): what it must hold, and what the page takes from it."""

import json
from pathlib import Path

import pytest

import catalog as catalog_module
from catalog import ALL_VIEW, Catalog, load_catalog
from config import settings
from services.geo import region_for_country


def test_shipped_catalog_is_valid():
    """The config folder loads, every region and the "all" view have sources, and every region has topic feeds."""
    shipped = load_catalog(Path(settings.CONFIG_DIR))
    assert shipped.view_ids[0] == ALL_VIEW and len(shipped.view_ids) == len(shipped.regions) + 1
    assert all(region.sources and region.topic_sources for region in shipped.regions)
    assert len(shipped.topic_view_ids) == len(shipped.view_ids) * len(shipped.topics)


def test_source_labels_must_be_topics(tmp_path):
    (tmp_path / "topics.yaml").write_text(json.dumps({"topics": [{"id": "sport", "label": "Sport"}]}))
    (tmp_path / "regions.yaml").write_text(json.dumps({"regions": [{"id": "asia", "label": "Asia"}]}))
    (tmp_path / "sources.yaml").write_text(json.dumps({  # JSON is YAML too
        "regions": {"asia": {"sources": [{"name": "A", "url": "https://a", "weight": 1.0}],
                             "topic_sources": [{"name": "B", "url": "https://b", "label": "gossip"}]}},
        "extras": [],
    }))
    with pytest.raises(ValueError, match="gossip"):
        load_catalog(tmp_path)


def test_sources_must_name_exactly_the_regions(tmp_path):
    (tmp_path / "topics.yaml").write_text(json.dumps({"topics": []}))
    (tmp_path / "regions.yaml").write_text(json.dumps({"regions": [{"id": "asia", "label": "Asia"}]}))
    (tmp_path / "sources.yaml").write_text(json.dumps({
        "regions": {"asai": {"sources": [{"name": "A", "url": "https://a", "weight": 1.0}]}}, "extras": []}))
    with pytest.raises(ValueError, match=r"missing \['asia'\], unknown \['asai'\]"):
        load_catalog(tmp_path)


def test_the_starting_tab_comes_from_the_regions_countries(monkeypatch):
    monkeypatch.setattr(catalog_module, "_catalog", Catalog(regions=[
        {"id": "south", "label": "South", "countries": "IN LK", "sources": [{"name": "A", "url": "https://a", "weight": 1.0}]},
    ], extras=[]))
    assert region_for_country("lk") == "south" and region_for_country("US") == "all" and region_for_country(None) == "all"
