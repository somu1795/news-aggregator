"""
Settings of the app's services (API, refresher, summarizer), from environment
variables or a .env file: a single, type-safe source of truth, and the only
place their defaults are written (docker-compose.yml passes them by name).

What the site is about lives in its config folder (CONFIG_DIR), not here: its
regions, topics and sources (catalog.py), and its name and prompts (site_config.py).
"""
from pathlib import Path
from typing import List, Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # --- Page ---
    DEFAULT_THEME: Literal["light", "dark"] = "light"  # Until a visitor toggles it with "d"

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
    MAX_HEADLINES: int = 100  # Per view (each region, and "all")
    # Views are built this many items longer, so that once each story is shown only once
    # (its other headlines folded under it, see services/story_groups.py) they still fill up
    STORY_GROUP_HEADROOM: int = 40
    STORY_GROUP_TIMEOUT: float = 60.0  # The first grouping after a start embeds every headline
    MAX_RELATED: int = 8  # Other outlets' headlines kept under a story
    MAX_HEADLINE_AGE_HOURS: int = 48  # Older items are dropped
    MAX_PER_PUBLISHER: int = 3  # Cap per publisher in a view (single-publisher feeds are bounded by their weight instead)
    REQUEST_TIMEOUT: float = 10.0
    # How feed and article requests identify themselves: a current desktop Chrome, as sites treat
    # old browser versions as suspicious. Keep it within a few versions of Chrome's stable release
    # (scripts/check-updates.py reports when it falls behind). Used by every outgoing request
    # that should look like a browser: feeds, articles and the Google link resolver.
    BROWSER_USER_AGENT: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
    )
    FETCH_RETRIES: int = 3
    # Polite polling (services/feeds/poller.py FeedPoller.fetch)
    FEED_MAX_INTERVAL_SECONDS: int = 1800  # A feed that keeps bringing nothing new is fetched at most this rarely
    FEED_BACKOFF_SECONDS: int = 600  # A host that answers 429/503 without Retry-After is paused this long, doubling
    FEED_BACKOFF_MAX_SECONDS: int = 7200
    FEED_JITTER_SECONDS: float = 10.0  # Each request starts after a random delay of up to this
    BACKOFF_BASE: int = 2

    # --- Tagger (topic views) ---
    TAGGER_URL: str = "http://tagger:8000"
    TAGGER_TIMEOUT: float = 90.0  # The first refresh embeds every headline (~5000)

    # --- Site icons (publisher favicons on the page) ---
    ICON_SOURCE_URL: str = "https://icons.duckduckgo.com/ip3/{host}.ico"
    # For a website without an icon there or on its own pages; redrawn as pixel art. Empty disables it.
    ICON_FALLBACK_URL: str = "https://www.google.com/s2/favicons?domain={host}&sz=64"
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
    # The same proxy without ad, tracker and video domains: tried first. Empty: always SUMMARIZER_BROWSER_PROXY.
    SUMMARIZER_BROWSER_FILTERED_PROXY: str = ""
    SUMMARIZER_BROWSER2_URL: str = ""  # A second browser (Byparr, same API), tried when the first fails
    SUMMARY_BROWSER_TIMEOUT: float = 60.0  # Per page, challenge included
    STORY_JUDGE_BATCH: int = 20  # Unsure story pairs asked for at a time (~0.1 s each on the LLM)
    STORY_JUDGE_SECONDS: float = 240.0  # Longest the LLM judges pairs before looking for articles again
    BROWSER_HOST_TTL_SECONDS: int = 7 * 86400  # A blocking site goes straight to the browser for this long
    FEED_TEXT_TTL_SECONDS: int = 2 * 86400  # The feed's own text of published items, for the summarizer

    # --- Archive (every published headline, kept; see services/archive.py) ---
    ARCHIVE_PATH: str = ""  # A SQLite file, written by the refresher, searched by the app; empty disables the archive
    ARCHIVE_SNAPSHOT_SECONDS: int = 3600  # How often each view's order is recorded
    SEARCH_TYPO_FALLBACK: bool = True  # Searching older headlines: a word nothing starts with is searched as the closest known word

    # --- The site's own files: regions, topics, sources, name, prompts (see catalog.py, site_config.py) ---
    # In the containers, docker-compose.yml mounts the repo's config folder (or CONFIG_DIR) here
    CONFIG_DIR: str = str(Path(__file__).resolve().parent.parent / "config")

    @property
    def allowed_hosts_list(self) -> List[str]:
        """Returns parsed ALLOWED_HOSTS as a list, or ['*'] in debug mode."""
        if self.DEBUG:
            return ["*"]
        return [h.strip() for h in self.ALLOWED_HOSTS.split(",")]


# Create a single, importable instance of the settings
settings = Settings()
