"""
Per-process in-memory copy of the latest snapshot of each view ("all" and
each region).

Each API worker re-reads a view's snapshot from Redis at most once every
LOCAL_CACHE_SECONDS, so request volume never translates into Redis or
upstream load. If Redis is unavailable, the last copy keeps being served.
"""

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Dict, Iterable, Optional

import redis.asyncio as aioredis
import structlog

from config import settings, snapshot_key
from metrics import REDIS_ERRORS

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Snapshot:
    body: str  # Serialized APIResponse, served as-is
    fetched_at: float
    expires_at: float


class _ViewState:
    def __init__(self) -> None:
        self.snapshot: Optional[Snapshot] = None
        self.checked_at = float("-inf")
        self.lock = asyncio.Lock()

    def is_fresh(self) -> bool:
        # With nothing to serve yet, always re-check so the first snapshot is picked up immediately
        return self.snapshot is not None and time.monotonic() - self.checked_at < settings.LOCAL_CACHE_SECONDS


class HeadlinesCache:
    def __init__(self, redis_client: aioredis.Redis, views: Iterable[str]) -> None:
        self._redis = redis_client
        self._views: Dict[str, _ViewState] = {view: _ViewState() for view in views}

    async def get(self, view: str) -> Optional[Snapshot]:
        """Returns the latest snapshot of a known view, or None if none has ever been published."""
        state = self._views[view]
        if state.is_fresh():
            return state.snapshot
        if state.lock.locked() and state.snapshot is not None:
            # Another request is already re-syncing; serve the current copy rather than
            # queueing behind it (a hung Redis would otherwise stall every request)
            return state.snapshot
        async with state.lock:  # One Redis read per view per process at a time
            if state.is_fresh():
                return state.snapshot
            try:
                raw = await self._redis.get(snapshot_key(view))
            except aioredis.RedisError as e:
                REDIS_ERRORS.labels(operation="read_headlines").inc()
                logger.warning("Redis read failed, serving in-memory snapshot", view=view, error=str(e))
                raw = None
            # Also set on failure, so a Redis outage costs one attempt per interval, not per request
            state.checked_at = time.monotonic()
            if raw and (state.snapshot is None or raw != state.snapshot.body):
                parsed = json.loads(raw)
                state.snapshot = Snapshot(raw, parsed["fetched_at"], parsed["expires_at"])
            return state.snapshot

    async def warm(self) -> None:
        """Loads every view, so each worker can keep serving if Redis later goes down."""
        for view in self._views:
            await self.get(view)
