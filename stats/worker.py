"""
The stats service: reads the stream of fetched headlines (app/services/stats_stream.py),
scores and places each one, groups them into stories in its own SQLite file, and
writes heatmap.json, which Caddy serves. Nothing calls it, and nothing waits for it:
when it stops, the map keeps its last file and says how old it is, and it catches
up from the stream when it starts again.
"""

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import redis

from embedder import Embedder
from models import LinearModel, is_relevant
from places import Places, clean_title
from scoring import DAY, build_output, red_dots
from store import RETENTION_DAYS, Store
from stories import StoryIndex

logger = logging.getLogger("stats")

STREAM = "stats:items"
BATCH = 2000
LOOP_SECONDS = 60
PUBLISH_SECONDS = 300
MAX_STRESS = 3.0
MAX_DATE_AGE = 7 * DAY  # A headline dated before this isn't news now: it's seen, and not counted
DROPPED_LABELS = {"sport", "culture", "tech", "science", "nature"}
WEB_LINK = ("https://", "http://")  # Nothing else is ever kept


@dataclass
class State:
    store: Store
    places: Places
    embedder: Optional[Embedder]
    stress: LinearModel
    relevant: LinearModel
    index: StoryIndex
    public: Path
    last_publish: float = 0.0
    last_prune: float = 0.0


def parse_entry(entry_id: str, fields: Dict[str, str]) -> Optional[dict]:
    """A stream entry as an item, or None if it can't be used."""
    link, source = fields.get("link", ""), fields.get("source", "")
    title = clean_title(fields.get("title", ""), source)
    if not title or not source or not link.startswith(WEB_LINK):
        return None
    arrived = int(entry_id.split("-")[0]) / 1000
    try:
        published = float(fields.get("published", ""))
    except ValueError:
        published = arrived
    when = published if arrived - MAX_DATE_AGE <= published <= arrived else arrived
    return {"title": title, "link": link, "source": source, "label": fields.get("label", ""), "when": when,
            "stale": published < arrived - MAX_DATE_AGE}


def _ingest(items: List[dict], vectors: np.ndarray, state: State) -> int:
    """Stores a batch of new headlines; returns how many counted towards a story."""
    if not items:
        return 0
    kept = is_relevant(state.relevant, vectors)
    stress = np.clip(state.stress.score(vectors), 0.0, MAX_STRESS)
    counted = 0
    for item, vector, keep, score in zip(items, vectors, kept, stress):
        located = state.places.locate(item["title"], item["source"]) if keep else None
        if located is None or not located.countries:
            state.store.add_headline(item["link"], None, item["when"])
            continue
        story = state.index.match(vector, item["when"])
        if story is None:
            story = state.store.add_story(item["when"], float(score), item["title"], item["link"], item["source"], vector)
            state.index.insert(story, vector, item["when"])
        else:
            state.store.join_story(story, float(score))
        state.store.add_headline(item["link"], story, item["when"])
        state.store.add_outlet(story, item["source"])
        for country in located.countries:
            state.store.add_country(story, country, located.from_title, located.positions.get(country))
        counted += 1
    return counted


def _new_items(items: List[dict], state: State) -> List[dict]:
    fresh: Dict[str, dict] = {}
    for item in items:
        if item["label"] not in DROPPED_LABELS and item["link"] not in fresh and not state.store.seen(item["link"]):
            fresh[item["link"]] = item
    return list(fresh.values())


def _reset_index(state: State, now: float) -> None:
    index = StoryIndex()
    state.store.load_index(index, now)
    state.index = index


def process(entries: List[Tuple[str, Dict[str, str]]], state: State) -> int:
    """Stores a batch of stream entries, and the position after them, as one transaction."""
    parsed = [parse_entry(entry_id, fields) for entry_id, fields in entries]
    usable = [item for item in parsed if item is not None]
    if len(usable) < len(parsed):
        logger.warning("Skipped %d unusable stream entries", len(parsed) - len(usable))
    try:
        items = _new_items(usable, state)
        for item in items:
            if item["stale"]:
                state.store.add_headline(item["link"], None, item["when"])
        items = [item for item in items if not item["stale"]]
        vectors = state.embedder.embed([item["title"] for item in items]) if items else None
        counted = _ingest(items, vectors, state)
        state.store.set_last_id(entries[-1][0])
        state.store.commit()
    except Exception:
        state.store.rollback()
        _reset_index(state, time.time())
        raise
    return counted


def ingest_rows(rows: List[dict], state: State) -> int:
    """Stored headlines with their embeddings, from backfill.py, as one transaction."""
    items = [{"title": clean_title(r["title"], r["source"]), "link": r["link"], "source": r["source"], "label": r["label"] or "",
              "when": r["first_seen"], "vector": r["vector"]} for r in rows if (r["link"] or "").startswith(WEB_LINK)]
    items = _new_items(items, state)
    if items:
        state.index.expire(items[0]["when"])
    vectors = np.stack([item["vector"] for item in items]) if items else None
    counted = _ingest(items, vectors, state)
    state.store.commit()
    return counted


def _entry_key(entry_id: str) -> Tuple[int, ...]:
    return tuple(int(part) for part in entry_id.split("-"))


def read_entries(client: redis.Redis, last_id: str) -> List[Tuple[str, Dict[str, str]]]:
    if last_id != "0":
        try:
            first = client.xinfo_stream(STREAM)["first-entry"]
        except redis.ResponseError:  # No stream yet
            first = None
        if first and _entry_key(first[0]) > _entry_key(last_id):
            logger.warning("Stats stream lost entries while the worker was away", extra={"position": last_id})
    found = client.xread({STREAM: last_id}, count=BATCH)
    return found[0][1] if found else []


def write_output(output: dict, path: Path) -> None:
    """Replaces the file whole, so a reader never sees half of it."""
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
    temporary.rename(path)


def tick(client: redis.Redis, state: State, now: float) -> None:
    """One pass: new headlines, then the map, when it's due or a new red dot appears."""
    state.index.expire(now)
    if now - state.last_prune >= DAY:
        state.store.prune(now)
        state.store.commit()
        state.last_prune = now
    entries = read_entries(client, state.store.last_id())
    counted = process(entries, state) if entries else 0
    due = now - state.last_publish >= PUBLISH_SECONDS
    if counted or due:
        stories = state.store.stories_since(now - RETENTION_DAYS * DAY)
        held = state.store.held_dots()
        dots = red_dots(stories, held, now)
        try:
            state.store.save_dots(dots)
            if due or set(dots) - set(held):
                write_output(build_output(stories, dots, now), state.public)
                state.last_publish = now
            state.store.commit()
        except Exception:
            state.store.rollback()
            raise


def build_state(embedder: Optional[Embedder]) -> State:
    here = Path(__file__).parent
    state = State(
        store=Store(Path(os.environ.get("STATS_DB", "/data/stats.db"))),
        places=Places.load(here / "data"), embedder=embedder,
        stress=LinearModel.load(here / "model/stress.json"), relevant=LinearModel.load(here / "model/relevant.json"),
        index=StoryIndex(), public=Path(os.environ.get("STATS_PUBLIC", "/public/heatmap.json")),
    )
    state.store.load_index(state.index, time.time())
    return state


def connect(url: str) -> redis.Redis:
    return redis.Redis.from_url(url, socket_timeout=10, socket_connect_timeout=5, decode_responses=True,
                                encoding_errors="replace")  # One corrupt entry mustn't stop the stream


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    state = build_state(Embedder(Path(os.environ.get("STATS_MODEL_DIR", "/models/multilingual-e5-small"))))
    client = connect(os.environ["REDIS_URL"])
    healthy = True
    logger.info("Stats worker started")
    while True:
        try:
            tick(client, state, time.time())
            if not healthy:
                logger.info("Redis is back")
            healthy = True
        except redis.RedisError as e:
            if healthy:  # Log the change, not every retry
                logger.warning("Redis unavailable, will retry: %s", e)
            healthy = False
        except Exception:
            logger.exception("Stats pass failed")
        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    main()
