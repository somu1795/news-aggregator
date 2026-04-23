"""
Centralized configuration for the News Aggregator application using Pydantic.

This module defines a `Settings` class that loads configuration from environment
variables and a .env file, providing a single, type-safe source of truth for all
application settings.
"""

from typing import Dict, List

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """
    Application settings, loaded from environment variables or .env file.
    Pydantic automatically matches environment variables to the field names (case-insensitive).
    """

    # --- Application Metadata ---
    APP_VERSION: str = "2.0.0"

    # --- Environment-specific Settings ---
    DEBUG: bool = Field(False, env="DEBUG")
    ENABLE_API_DOCS: bool = Field(False, env="ENABLE_API_DOCS")
    LOG_LEVEL: str = Field("INFO", env="LOG_LEVEL")
    ADMIN_API_KEY: str = Field("changeme", env="ADMIN_API_KEY")

    # --- Allowed Hosts (security) ---
    ALLOWED_HOSTS: str = Field("localhost", env="ALLOWED_HOSTS")

    # --- Rate Limiting ---
    API_RATE_LIMIT: str = Field("100/minute", env="API_RATE_LIMIT")

    # --- Redis Settings ---
    REDIS_URL: str = Field("redis://redis:6379", env="REDIS_URL")
    CACHE_KEY: str = "headlines:v2"
    CACHE_TTL_SECONDS: int = 60
    LOCK_KEY: str = f"{CACHE_KEY}:lock"
    LOCK_TIMEOUT_SECONDS: int = 15  # How long the lock is held
    LOCK_WAIT_TIMEOUT_SECONDS: int = 12  # How long a follower waits for the lock

    # --- Feed Fetching Settings ---
    MAX_HEADLINES: int = 30
    REQUEST_TIMEOUT: float = 10.0
    FETCH_RETRIES: int = 3
    BACKOFF_BASE: int = 2

    # --- News Sources ---
    NEWS_SOURCES: Dict[str, str] = {
        "Google News (Custom Search)": 'https://news.google.com/rss/search?q=news%20from%20world%20-ndtv%20-hindustan%20times%20-India%20Today%20-Horseed&hl=en-US&gl=US&ceid=US%3Aen',
        "Google News (Science)": 'https://news.google.com/rss/topics/CAAqKggKIiRDQkFTRlFvSUwyMHZNRFp0Y1RjU0JXVnVMVWRDR2dKSlRpZ0FQAQ?hl=en-US&gl=US&ceid=US%3Aen',
    }

    # --- Source Weighting ---
    SOURCE_WEIGHTS: Dict[str, float] = {
        "Google News (Custom Search)": 0.7,
        "Google News (Science)": 0.3,
    }

    @property
    def allowed_hosts_list(self) -> List[str]:
        """Returns parsed ALLOWED_HOSTS as a list, or ['*'] in debug mode."""
        if self.DEBUG:
            return ["*"]
        return [h.strip() for h in self.ALLOWED_HOSTS.split(",")]

    class Config:
        # Pydantic will look for a .env file and load environment variables from it.
        env_file = ".env"
        env_file_encoding = "utf-8"


# Create a single, importable instance of the settings
settings = Settings()

# --- Validation ---
if not abs(sum(settings.SOURCE_WEIGHTS.values()) - 1.0) < 1e-9:
    raise ValueError("SOURCE_WEIGHTS must sum to 1.0")

if not all(key in settings.NEWS_SOURCES for key in settings.SOURCE_WEIGHTS.keys()):
    raise ValueError("All keys in SOURCE_WEIGHTS must also be present in NEWS_SOURCES")
