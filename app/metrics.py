"""
Prometheus metric declarations for the News Aggregator.

All application metrics are defined here as a single source of truth,
imported by the modules that need to record them.
"""

from prometheus_client import Counter, Gauge, Enum

CACHE_HITS = Counter(
    "headlines_cache_hits_total",
    "Total cache hits for headlines",
    ["source"],
)
CACHE_MISSES = Counter(
    "headlines_cache_misses_total",
    "Total cache misses for headlines",
    ["reason"],
)
HEADLINES_FETCHED = Gauge(
    "headlines_fetched_count",
    "Number of headlines fetched in the last live pull",
)
REDIS_ERRORS = Counter(
    "redis_errors_total",
    "Total number of Redis errors encountered",
    ["operation"],
)
FALLBACK_FETCHES = Counter(
    "fallback_fetches_total",
    "Total number of fallback fetches performed",
    ["reason"],
)
FALLBACK_REASON_ENUM = Enum(
    "fallback_reason",
    "Reason for a fallback fetch",
    states=["redis_error", "lock_timeout"],
)
