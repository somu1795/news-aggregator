"""
The site's catalog: its regions, topics and news sources, read from its config
folder (settings.CONFIG_DIR): regions.yaml, topics.yaml and sources.yaml (each
file's header describes its format). Loaded once, on first use (get_catalog());
each service asks for it at startup, so a broken file stops it with its error
rather than leaving a half-configured site running.
"""
from pathlib import Path
from typing import Dict, List, Optional

import yaml
from pydantic import BaseModel, Field, field_validator

from config import settings

ALL_VIEW = "all"
OTHER_LABEL = "other"


class NewsSource(BaseModel):
    name: str
    url: str
    weight: float  # Share of a view's headlines taken from this source
    # For the tagger: "other" for general national news, or the topic of a single-topic feed
    # (its items are always tagged with it, as for a TopicSource)
    label: Optional[str] = None
    max_items: Optional[int] = None  # At most this many of its items per view (for a feed whose quality varies)


class TopicSource(BaseModel):
    """A feed about one topic (e.g. a Google News Sports section). Only shown in topic views."""
    name: str
    url: str
    label: str  # Its topic: every item is tagged with it, and it trains the tagger
    max_items: Optional[int] = None  # At most this many of its items per view (for a feed whose quality varies)


class Topic(BaseModel):
    id: str = Field(pattern=r"^[a-z]+$")  # Must be one of the tagger's topics (tagger/classifier.py TOPICS)
    label: str
    # Its views keep stories this long instead of MAX_HEADLINE_AGE_HOURS (e.g. good news
    # doesn't go stale, and its few feeds publish a handful of stories a day)
    max_age_hours: Optional[int] = None
    # Only items from feeds about it, never ones the tagger's model tagged: for a topic the
    # model can't tell reliably ("uplifting" is a tone; its tags were about half wrong)
    feed_only: bool = False
    # Its feeds' items the tagger's model gives less than this probability of being about it are
    # left out (a section feed's misfiled items: a cricket feature in a Technology section)
    min_feed_score: Optional[float] = Field(default=None, ge=0, le=1)


class Region(BaseModel):
    id: str = Field(pattern=r"^[a-z]+$")
    label: str
    sources: List[NewsSource]  # Weights sum to 1.0
    topic_sources: List[TopicSource] = []
    countries: List[str] = []  # Visitors from these (ISO 3166 codes) start on its tab (services/geo.py)
    places: List[str] = []  # Names that put a worldwide feed's story in its topic views (services/places.py)
    abbreviations: List[str] = []  # Matched only as written ("US"): in lowercase they're ordinary words

    @field_validator("countries", mode="before")
    @classmethod
    def _split_codes(cls, value):
        return value.split() if isinstance(value, str) else value  # regions.yaml writes them space-separated


class Catalog(BaseModel):
    """All news sources: one source list per region, plus extras mixed into every view."""
    regions: List[Region]
    extras: List[NewsSource]  # Their weights are shares of every view; the view's news gets the rest
    topics: List[Topic] = []  # Each region (and "all") also has one view per topic
    topic_extras: List[TopicSource] = []  # Topic feeds that belong to no region; in every region's topic views

    @property
    def view_ids(self) -> List[str]:
        return [ALL_VIEW] + [region.id for region in self.regions]

    @property
    def topic_view_ids(self) -> List[str]:
        return [topic_view(view, topic.id) for view in self.view_ids for topic in self.topics]

    @property
    def topic_feeds(self) -> List[TopicSource]:
        return [s for region in self.regions for s in region.topic_sources] + self.topic_extras

    @property
    def labelled_sources(self) -> List[NewsSource | TopicSource]:
        """Every source whose items carry a label (a topic or "other") for the tagger."""
        news = [s for region in self.regions for s in region.sources] + self.extras
        return [s for s in news if s.label] + self.topic_feeds


def topic_view(view: str, topic: str) -> str:
    """Id of a region's (or "all"'s) view of one topic, e.g. "asia:sport"."""
    return f"{view}:{topic}"


def _read(path: Path) -> Dict:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path} must be a YAML mapping")
    return data


def load_catalog(config_dir: Path) -> Catalog:
    """Loads and validates the catalog from a config folder's regions.yaml, topics.yaml and sources.yaml."""
    regions = _read(config_dir / "regions.yaml").get("regions") or []
    topics = _read(config_dir / "topics.yaml").get("topics") or []
    sources_path = config_dir / "sources.yaml"
    sources = _read(sources_path)
    by_region = sources.get("regions") or {}
    ids = [region.get("id") for region in regions]
    if set(by_region) != set(ids):
        raise ValueError(f"{sources_path} must list sources for exactly the regions of regions.yaml: "
                         f"missing {sorted(set(ids) - set(by_region))}, unknown {sorted(set(by_region) - set(ids))}")
    catalog = Catalog(
        regions=[{**region, **by_region[region["id"]]} for region in regions],
        extras=sources.get("extras") or [],
        topics=topics,
        topic_extras=sources.get("topic_extras") or [],
    )
    check_catalog(catalog)
    return catalog


def check_catalog(catalog: Catalog) -> None:
    """Raises ValueError if the catalog breaks a rule the files' headers state."""
    if not catalog.regions:
        raise ValueError("regions.yaml must define at least one region")
    ids = [region.id for region in catalog.regions]
    if len(set(ids)) != len(ids) or ALL_VIEW in ids:
        raise ValueError(f"Region ids must be unique and not '{ALL_VIEW}'")
    names = ([s.name for r in catalog.regions for s in r.sources] + [s.name for s in catalog.extras]
             + [s.name for s in catalog.topic_feeds])
    if len(set(names)) != len(names):
        raise ValueError("Source names must be unique")
    for region in catalog.regions:
        if not region.sources or abs(sum(s.weight for s in region.sources) - 1.0) > 1e-6:
            raise ValueError(f"Source weights of region '{region.id}' must sum to 1.0")
    if not 0 <= sum(s.weight for s in catalog.extras) < 1:
        raise ValueError("extras weights must sum to less than 1.0")
    topic_ids = {topic.id for topic in catalog.topics}
    labels = {s.label for s in catalog.labelled_sources}
    if not labels <= topic_ids | {OTHER_LABEL}:
        raise ValueError(f"Source labels must be topic ids or '{OTHER_LABEL}', got {sorted(labels - topic_ids - {OTHER_LABEL})}")
    countries = [code for region in catalog.regions for code in region.countries]
    if len(set(countries)) != len(countries):
        raise ValueError("A country can start on one region only (regions.yaml countries)")
    places = [name.lower() for region in catalog.regions for name in region.places]
    if len(set(places)) != len(places):
        raise ValueError("A place can belong to one region only (regions.yaml places)")


_catalog: Optional[Catalog] = None


def get_catalog() -> Catalog:
    """The site's catalog, loaded from settings.CONFIG_DIR on first use."""
    global _catalog
    if _catalog is None:
        _catalog = load_catalog(Path(settings.CONFIG_DIR))
    return _catalog
