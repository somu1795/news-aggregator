"""
Per-process in-memory copy of the latest snapshot of each view ("all" and
each region), and of all of them combined for the page's search.

Each API worker re-reads a view's snapshot from Redis at most once every
LOCAL_CACHE_SECONDS, so request volume never translates into Redis or
upstream load. If Redis is unavailable, the last copy keeps being served.

Each snapshot is also compressed, once, when it arrives (in a thread), and
served that way to clients that accept it: Caddy would otherwise compress the
same body again for every request, which was the limit under load (4 CPUs of
Caddy to 0.8 of the app, on the search index). Until the copies are ready, or
if making one fails, the snapshot is served as it is and Caddy compresses it.
"""

import asyncio
import gzip
import json
import time
from compression import zstd
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Set

import redis.asyncio as aioredis
import structlog

from config import settings
from keys import snapshot_key

logger = structlog.get_logger(__name__)


# What a snapshot is also kept as, in our order of preference (Caddy's too). The strongest levels:
# each is made once per snapshot, off the event loop (the search index's 1.3 MB: zstd 0.5 s and
# 342 KB, gzip 0.07 s and 427 KB; Caddy's per-request levels gave 431 KB and 443 KB).
_ENCODERS: Dict[str, Callable[[bytes], bytes]] = {
    "zstd": lambda body: zstd.compress(body, level=19),
    "gzip": lambda body: gzip.compress(body, compresslevel=9, mtime=0),
}


@dataclass(frozen=True)
class Snapshot:
    body: str  # Serialized APIResponse, served as-is
    fetched_at: float
    expires_at: float
    # The body by Content-Encoding; each appears when it's ready (see the module's docstring)
    encoded: Dict[str, bytes] = field(default_factory=dict, compare=False, repr=False)


def accepted_encoding(accept_encoding: str, available: Iterable[str]) -> Optional[str]:
    """
    The first of `available` that a client sending this Accept-Encoding header takes, or None
    for the body as it is. RFC 9110: names in any case, `;q=0` refuses one, `*` is every
    encoding not named. A weight that can't be read refuses its encoding.
    """
    weights: Dict[str, float] = {}
    for part in accept_encoding.split(","):
        name, *parameters = (piece.strip().lower() for piece in part.split(";"))
        if not name:
            continue
        weight = 1.0
        for parameter in parameters:
            if parameter.startswith("q="):
                try:
                    weight = float(parameter[2:])
                except ValueError:
                    weight = 0.0
        weights[name] = weight
    for encoding in available:
        if weights.get(encoding, weights.get("*", 0.0)) > 0:
            return encoding
    return None


def _compress(snapshot: Snapshot) -> None:
    """Runs in a thread. One encoding failing leaves the others, and the snapshot served as it is."""
    body = snapshot.body.encode()
    for encoding, encode in _ENCODERS.items():
        try:
            snapshot.encoded[encoding] = encode(body)
        except Exception as e:
            logger.warning("Could not compress a snapshot, serving it uncompressed", encoding=encoding, error=str(e))


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
        self._search_index: Optional[Snapshot] = None
        self._search_sources: List[Optional[Snapshot]] = []  # The view snapshots it was built from
        self._compressing: Set[asyncio.Task] = set()  # Held here: the loop itself doesn't keep a running task

    def _new_snapshot(self, body: str, fetched_at: float, expires_at: float) -> Snapshot:
        """The snapshot, served as it is from now and compressed in the background."""
        snapshot = Snapshot(body, fetched_at, expires_at)
        task = asyncio.create_task(asyncio.to_thread(_compress, snapshot))
        self._compressing.add(task)
        task.add_done_callback(self._compressing.discard)
        return snapshot

    async def compressed(self) -> None:
        """Returns once every snapshot so far has its compressed copies."""
        while self._compressing:
            await asyncio.gather(*self._compressing)

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
                logger.warning("Redis read failed, serving in-memory snapshot", view=view, error=str(e))
                raw = None
            # Also set on failure, so a Redis outage costs one attempt per interval, not per request
            state.checked_at = time.monotonic()
            if raw and (state.snapshot is None or raw != state.snapshot.body):
                parsed = json.loads(raw)
                state.snapshot = self._new_snapshot(raw, parsed["fetched_at"], parsed["expires_at"])
            return state.snapshot

    async def search_index(self, views: List[str]) -> Optional[Snapshot]:
        """
        The views' items in one snapshot (models.SearchIndex), each link once with the views it's
        in, in the order given; None if none of them has a snapshot yet. Rebuilt only when one of
        the views has a new snapshot.
        """
        sources = [await self.get(view) for view in views]
        if len(sources) == len(self._search_sources) and all(a is b for a, b in zip(sources, self._search_sources)):
            return self._search_index
        published = [(view, snapshot) for view, snapshot in zip(views, sources) if snapshot is not None]
        if not published:
            return None
        items: Dict[str, Dict] = {}
        for view, snapshot in published:
            for item in json.loads(snapshot.body)["data"]:
                items.setdefault(item["link"], {**item, "views": []})["views"].append(view)
        fetched_at = max(snapshot.fetched_at for _, snapshot in published)
        expires_at = min(snapshot.expires_at for _, snapshot in published)
        body = json.dumps({"items": list(items.values()), "fetched_at": fetched_at, "expires_at": expires_at},
                          separators=(",", ":"), ensure_ascii=False)
        self._search_index, self._search_sources = self._new_snapshot(body, fetched_at, expires_at), sources
        return self._search_index

    async def warm(self) -> None:
        """Loads every view, so each worker can keep serving if Redis later goes down."""
        for view in self._views:
            await self.get(view)
