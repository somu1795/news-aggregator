"""
Headlines API routes.

GET /api/headlines?region=<view>[&topic=<topic>] — serves the latest snapshot
of a view ("all", the default, or a region id from sources.json), or of that
view's headlines about one topic, published by the refresher.
GET /api/search-index — every view's headlines in one response, each link
once with the views it's in: what the page's search looks through.
These paths never fetch from upstream; they read a per-process in-memory copy
that is re-synced from Redis every few seconds.

Browsers cache a response until the next refresh is due (Cache-Control). The
CDN (CDN-Cache-Control, which Cloudflare honours and browsers ignore) keeps it
at least EDGE_MIN_SECONDS, so near or past a refresh most requests still don't
reach the origin; it may then serve a stale copy while it re-fetches, or for a
day if the origin is unreachable.
"""

import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response

from config import ALL_VIEW, CATALOG, settings, topic_view
from deps import limiter
from models import APIResponse, SearchIndex
from services.headlines_cache import Snapshot

router = APIRouter()

EDGE_MIN_SECONDS = 60  # Edge copies may be up to this much older than the newest snapshot

# Every view in the page's tab order (each region, then its topics), as the search index lists them
_SEARCH_VIEWS = [v for view in CATALOG.view_ids for v in [view] + [topic_view(view, t.id) for t in CATALOG.topics]]

_NOT_YET = "Headlines are not available yet, please try again shortly"


@router.api_route("/api/headlines", response_model=APIResponse, methods=["GET", "HEAD"])
@limiter.limit(settings.API_RATE_LIMIT)
async def get_headlines(request: Request, region: str = ALL_VIEW, topic: Optional[str] = None):
    if region not in CATALOG.view_ids:
        raise HTTPException(status_code=404, detail=f"Unknown region; use one of: {', '.join(CATALOG.view_ids)}")
    topics = [t.id for t in CATALOG.topics]
    if topic is not None and topic not in topics:
        raise HTTPException(status_code=404, detail=f"Unknown topic; use one of: {', '.join(topics)}")
    snapshot = await request.app.state.headlines_cache.get(topic_view(region, topic) if topic else region)
    if snapshot is None:
        # Only before the refresher's first successful run against an empty Redis (for a
        # topic view: its first successful tagging)
        raise HTTPException(status_code=503, detail=_NOT_YET, headers={"Retry-After": "10"})
    return _cached(snapshot)


@router.api_route("/api/search-index", response_model=SearchIndex, methods=["GET", "HEAD"])
@limiter.limit(settings.API_RATE_LIMIT)
async def get_search_index(request: Request):
    snapshot = await request.app.state.headlines_cache.search_index(_SEARCH_VIEWS)
    if snapshot is None:
        raise HTTPException(status_code=503, detail=_NOT_YET, headers={"Retry-After": "10"})
    return _cached(snapshot)


def _cached(snapshot: Snapshot) -> Response:
    """The snapshot, cached until it's due to be replaced."""
    max_age = max(0, int(snapshot.expires_at - time.time()))
    return Response(
        content=snapshot.body,
        media_type="application/json",
        headers={
            "Cache-Control": f"public, max-age={max_age}",
            "CDN-Cache-Control": f"public, max-age={max(max_age, EDGE_MIN_SECONDS)}, "
                                 "stale-while-revalidate=60, stale-if-error=86400",
        },
    )
