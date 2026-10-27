"""
Tests for how settings reach the containers: docker-compose.yml passes them
from .env by name, and their defaults are written only in config.Settings.
"""

import re
from pathlib import Path

import yaml

from config import Settings

COMPOSE = Path(__file__).resolve().parents[2] / "docker-compose.yml"
APP_SERVICES = ("app", "refresher", "summarizer")


def environment(service: str) -> list:
    return yaml.safe_load(COMPOSE.read_text())["services"][service]["environment"]


def test_settings_passed_by_name_exist():
    fields = {name.upper() for name in Settings.model_fields}
    for service in APP_SERVICES:
        by_name = [entry for entry in environment(service) if "=" not in entry]
        assert set(by_name) <= fields, f"{service}: not settings: {sorted(set(by_name) - fields)}"


def test_compose_writes_no_second_default_for_a_setting():
    fields = {name.upper() for name in Settings.model_fields}
    for service in APP_SERVICES:
        for entry in environment(service):
            match = re.fullmatch(r"(\w+)=\$\{(\w+):-.*\}", entry)
            assert not (match and match.group(1) in fields), f"{service}: {entry} repeats a default of config.py"


def test_sources_must_name_exactly_the_regions(tmp_path):
    import json
    import pytest
    from catalog import load_catalog
    (tmp_path / "topics.yaml").write_text(json.dumps({"topics": []}))
    (tmp_path / "regions.yaml").write_text(json.dumps({"regions": [{"id": "asia", "label": "Asia"}]}))
    (tmp_path / "sources.yaml").write_text(json.dumps({
        "regions": {"asai": {"sources": [{"name": "A", "url": "https://a", "weight": 1.0}]}}, "extras": []}))
    with pytest.raises(ValueError, match=r"missing \['asia'\], unknown \['asai'\]"):
        load_catalog(tmp_path)


def test_the_starting_tab_comes_from_the_regions_countries(monkeypatch):
    import catalog as catalog_module
    from catalog import Catalog
    from services.geo import region_for_country
    monkeypatch.setattr(catalog_module, "_catalog", Catalog(regions=[
        {"id": "south", "label": "South", "countries": "IN LK", "sources": [{"name": "A", "url": "https://a", "weight": 1.0}]},
    ], extras=[]))
    assert region_for_country("lk") == "south" and region_for_country("US") == "all" and region_for_country(None) == "all"
