"""
Prometheus metric declarations shared by the API and the refresher.

The API runs several Uvicorn workers in Prometheus multi-process mode
(PROMETHEUS_MULTIPROC_DIR), so its /metrics aggregates all workers. The
refresher is a single process serving its own metrics on METRICS_PORT.

Only labeled metrics belong here: they create no series until first used, so
refresher-side metrics don't show up as zeros in the API's /metrics. The
refresher's unlabeled metrics are declared in refresher.py.
"""

from prometheus_client import Counter

REDIS_ERRORS = Counter(
    "redis_errors_total",
    "Total number of Redis errors encountered",
    ["operation"],
)

# --- Refresher-side (recorded by services the refresher uses) ---
REFRESHES = Counter(
    "headlines_refreshes_total",
    "Refresh cycles by result (success, empty, timeout, error)",
    ["result"],
)
FEED_FETCH_FAILURES = Counter(
    "feed_fetch_failures_total",
    "Feed fetches that failed after all retries",
    ["source"],
)
REDIRECT_RESOLUTIONS = Counter(
    "redirect_resolutions_total",
    "Google News link resolution outcomes (custom resolver)",
    ["outcome"],
)
REDIRECT_FALLBACKS = Counter(
    "redirect_fallbacks_total",
    "Fallback link resolutions, run only when the custom resolver failed (success, failed)",
    ["outcome"],
)
