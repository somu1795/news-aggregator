"""
Centralized configuration for the News Aggregator application using Pydantic.

This module defines a `Settings` class that loads configuration from environment
variables and a .env file, providing a single, type-safe source of truth for all
application settings. News sources live in a separate JSON file (`sources.json`)
so they can be edited without touching code.
"""
import hashlib
import json
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    id: str = Field(pattern=r"^[a-z]+$")  # Must be one of the tagger's topics (tagger/tagger.py TOPICS)
    label: str
    # Its views keep stories this long instead of MAX_HEADLINE_AGE_HOURS (e.g. good news
    # doesn't go stale, and its few feeds publish a handful of stories a day)
    max_age_hours: Optional[int] = None


class Region(BaseModel):
    id: str = Field(pattern=r"^[a-z]+$")
    label: str
    sources: List[NewsSource]  # Weights sum to 1.0
    topic_sources: List[TopicSource] = []


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


ALL_VIEW = "all"
OTHER_LABEL = "other"

# Paywalled sites: the page can open them via an archive, and the summarizer
# reads their articles from one when it can't read them directly.
PAYWALLED_HOSTS = [
    "nytimes.com", "wsj.com", "bloomberg.com", "economist.com",
    "washingtonpost.com", "ft.com", "businessinsider.com", "theathletic.com",
    "reuters.com", "thetimes.co.uk", "theatlantic.com",
]


def is_paywalled(host: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in PAYWALLED_HOSTS)


def topic_view(view: str, topic: str) -> str:
    """Id of a region's (or "all"'s) view of one topic, e.g. "asia:sport"."""
    return f"{view}:{topic}"


class Settings(BaseSettings):
    """
    Application settings, loaded from environment variables or .env file.
    Pydantic automatically matches environment variables to the field names (case-insensitive).
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # --- Application Metadata ---
    APP_VERSION: str = "2.0.0"

    # --- Environment-specific Settings ---
    DEBUG: bool = False
    ENABLE_API_DOCS: bool = False
    LOG_LEVEL: str = "INFO"
    ADMIN_API_KEY: str = "changeme"

    # --- Allowed Hosts (security) ---
    ALLOWED_HOSTS: str = "localhost"

    # --- Rate Limiting ---
    API_RATE_LIMIT: str = "100/minute"

    # --- Redis Settings ---
    REDIS_URL: str = "redis://redis:6379"
    HEADLINES_KEY: str = "headlines"  # Prefix: one snapshot per view at "headlines:<view>"; no TTL
    REFRESH_REQUEST_KEY: str = "headlines:refresh_requested"
    LOCAL_CACHE_SECONDS: float = 5.0  # How long each API worker reuses its in-memory snapshot

    # --- Refresher Settings ---
    # 84 feeds per cycle (18 from Google News): every 5 minutes keeps upstream load polite
    REFRESH_INTERVAL_SECONDS: int = 300
    REFRESH_TIMEOUT_SECONDS: int = 270  # Hard cap on a single refresh cycle (both phases)
    TOPIC_REFRESH_TIMEOUT_SECONDS: int = 150  # Cap on the topic views phase (tagging, link resolution)
    # Topic feeds (40+ Google News sections) change slowly: fetched every 30 minutes
    TOPIC_FEEDS_INTERVAL_SECONDS: int = 1800
    STALE_AFTER_SECONDS: int = 900  # /health reports the snapshot as stale beyond this age
    METRICS_PORT: int = 9100  # Prometheus port exposed by the refresher process

    # --- Feed Fetching & Mixing Settings ---
    MAX_HEADLINES: int = 80  # Per view (each region, and "all")
    # Views are built this many items longer, so that once each story is shown only once
    # (its other headlines folded under it, see services/story_groups.py) they still fill up
    STORY_GROUP_HEADROOM: int = 40
    STORY_GROUP_TIMEOUT: float = 60.0  # The first grouping after a start embeds every headline
    MAX_RELATED: int = 8  # Other outlets' headlines kept under a story
    MAX_HEADLINE_AGE_HOURS: int = 48  # Older items are dropped
    MAX_PER_PUBLISHER: int = 3  # Cap per publisher in a view (single-publisher feeds are bounded by their weight instead)
    REQUEST_TIMEOUT: float = 10.0
    FETCH_RETRIES: int = 3
    BACKOFF_BASE: int = 2

    # --- Tagger (topic views) ---
    TAGGER_URL: str = "http://tagger:8000"
    TAGGER_TIMEOUT: float = 90.0  # The first refresh embeds every headline (~5000)

    # --- Site icons (publisher favicons on the page) ---
    ICON_SOURCE_URL: str = "https://icons.duckduckgo.com/ip3/{host}.ico"
    ICON_TTL_SECONDS: int = 7 * 86400
    ICON_FAILED_TTL_SECONDS: int = 86400
    ICON_REFRESH_TIMEOUT_SECONDS: int = 60  # Cap on the icon step of a refresh cycle

    # --- Summarizer (article summaries) ---
    # An Ollama server; empty disables summaries. Set it in .env (e.g. a GPU machine on the tailnet).
    SUMMARIZER_LLM_URL: str = ""
    SUMMARIZER_MODEL: str = "qwen3:8b"
    SUMMARY_TOP_N: int = 20  # The first N items of every view get a summary
    SUMMARY_TTL_SECONDS: int = 7 * 86400
    SUMMARY_FAILED_TTL_SECONDS: int = 86400  # An article that couldn't be summarized isn't retried before this
    SUMMARY_INTERVAL_SECONDS: int = 60  # How often the summarizer looks for new top items
    SUMMARY_LLM_TIMEOUT: float = 180.0  # The first request also loads the model
    SUMMARY_MIN_TEXT_CHARS: int = 600  # Less is usually a paywall teaser or a video page
    SUMMARY_MAX_TEXT_CHARS: int = 6000  # ~1,500 tokens: enough for the gist, keeps prompts fast
    SUMMARY_RETRY_SECONDS: int = 3 * 3600  # Blocks and missing archive snapshots are often temporary
    # A headless browser (FlareSolverr) for sites that block plain requests, and the
    # proxy it must use (Smokescreen: public addresses only). Empty disables it.
    SUMMARIZER_BROWSER_URL: str = ""
    SUMMARIZER_BROWSER_PROXY: str = ""
    SUMMARIZER_BROWSER2_URL: str = ""  # A second browser (Byparr, same API), tried when the first fails
    SUMMARY_BROWSER_TIMEOUT: float = 60.0  # Per page, challenge included
    BROWSER_HOST_TTL_SECONDS: int = 7 * 86400  # A blocking site goes straight to the browser for this long
    FEED_TEXT_TTL_SECONDS: int = 2 * 86400  # The feed's own text of published items, for the summarizer

    # --- News Sources ---
    SOURCES_FILE: str = "sources.json"  # Relative to this directory, or an absolute path

    @property
    def allowed_hosts_list(self) -> List[str]:
        """Returns parsed ALLOWED_HOSTS as a list, or ['*'] in debug mode."""
        if self.DEBUG:
            return ["*"]
        return [h.strip() for h in self.ALLOWED_HOSTS.split(",")]


def load_catalog(path: Path) -> Catalog:
    """Loads and validates the news source catalog."""
    catalog = Catalog(**json.loads(path.read_text()))
    if not catalog.regions:
        raise ValueError(f"{path} must define at least one region")
    ids = [region.id for region in catalog.regions]
    if len(set(ids)) != len(ids) or ALL_VIEW in ids:
        raise ValueError(f"Region ids in {path} must be unique and not '{ALL_VIEW}'")
    names = ([s.name for r in catalog.regions for s in r.sources] + [s.name for s in catalog.extras]
             + [s.name for s in catalog.topic_feeds])
    if len(set(names)) != len(names):
        raise ValueError(f"Source names in {path} must be unique")
    for region in catalog.regions:
        if not region.sources or abs(sum(s.weight for s in region.sources) - 1.0) > 1e-6:
            raise ValueError(f"Source weights of region '{region.id}' in {path} must sum to 1.0")
    if not 0 <= sum(s.weight for s in catalog.extras) < 1:
        raise ValueError(f"extras weights in {path} must sum to less than 1.0")
    topic_ids = {topic.id for topic in catalog.topics}
    labels = {s.label for s in catalog.labelled_sources}
    if not labels <= topic_ids | {OTHER_LABEL}:
        raise ValueError(f"Source labels in {path} must be topic ids or '{OTHER_LABEL}', got {sorted(labels - topic_ids - {OTHER_LABEL})}")
    return catalog


# Create a single, importable instance of the settings
settings = Settings()

CATALOG = load_catalog(Path(__file__).parent / settings.SOURCES_FILE)


def summary_key(link: str) -> str:
    """Redis key holding an article's summary (or a failure marker), by its link."""
    return "summary:" + hashlib.sha256(link.encode()).hexdigest()[:32]


def feed_text_key(link: str) -> str:
    """Redis key holding the text the feed itself carried for an article, by its link."""
    return "feed-text:" + hashlib.sha256(link.encode()).hexdigest()[:32]


def browser_host_key(host: str) -> str:
    """Redis key marking a site that blocks plain requests (its articles are read with the browser)."""
    return f"browser-host:{host}"


def snapshot_key(view: str) -> str:
    """Redis key holding the latest snapshot of a view ("all", a region id, or a topic_view())."""
    return f"{settings.HEADLINES_KEY}:{view}"
