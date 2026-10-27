"""
Topic tagger service.

Embeds headlines with bge-small-en-v1.5 (on the Intel NPU via OpenVINO, or the
CPU if no NPU is available) and tags them with a classifier trained on our own
stored headlines.

POST /tag     {"items": [{"title", "source", "link", "label"}]} -> {"tags": [[topic, ...], ...], "model": version}
POST /group   {"items": [{"title", "source"}]} -> {"groups": [group number, ...]}: the same number for
              headlines about the same story (see Tagger.group)
GET  /story-candidates?limit=N -> {"pairs": [{"a", "b", "similarity"}]}: pairs grouping was unsure of,
              most important first, for a judge (the summarizer's LLM) to decide
POST /story-verdicts {"verdicts": [{"a", "b", "same", "model"}]}: its decisions, used by every later grouping
GET  /health  device, model version, stored headline count

Storage: every headline is stored once in SQLite (DATA_DIR/tagger.db) with its
embedding, the label its feed implies (a Sports section -> "sport", a national
news section -> "other", most feeds -> none) and the model's tags. A headline
is embedded only the first time it is seen.

Code: this file (the API, topic tagging, retraining), embedder.py (the models
on the NPU), classifier.py (training and scoring), store.py (the database),
stories.py (story grouping and the judge's pairs).

Training: a logistic regression on the stored embeddings of the newest
TRAIN_MAX_ROWS labelled headlines (data/seed.json, then what the feeds add). It is retrained every
RETRAIN_INTERVAL_HOURS; the new classifier replaces the current one only if it
scores at least as well on the hand-labelled evaluation set (data/eval.json),
which is never trained on.
"""

import asyncio
import ctypes
import json
import logging
import os
import pickle
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI
from pydantic import BaseModel

from classifier import OTHER, TOPICS, Classifier, evaluate, train
from embedder import Embedder
from stories import StoryGrouping
from store import clean_title, connect, from_blobs, to_blob

logger = logging.getLogger("tagger")

MODEL_DIR = Path(os.environ.get("MODEL_DIR", "/models/bge-small-en-v1.5"))
# Story grouping needs embeddings that tell stories apart: the topic model is fine-tuned to
# put every football story together. multilingual-e5-small also matches across languages.
STORY_MODEL_DIR = Path(os.environ.get("STORY_MODEL_DIR", "/models/multilingual-e5-small"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DEVICE = os.environ.get("TAGGER_DEVICE", "NPU")
RETRAIN_INTERVAL_HOURS = float(os.environ.get("RETRAIN_INTERVAL_HOURS", "24"))
# Retraining uses the newest labelled headlines only: ~14,000 arrive a day, and all
# of them (float32 embeddings) would outgrow the container's memory within months
TRAIN_MAX_ROWS = int(os.environ.get("TRAIN_MAX_ROWS", "100000"))
BUNDLED = Path(__file__).parent / "data"


class Tagger(StoryGrouping):
    """
    All database and embedding work runs on one worker thread (one SQLite
    connection, one NPU request at a time); retraining runs on another thread
    with its own read-only connection and swaps the classifier when done.
    """

    def __init__(self, embedder: Embedder, data_dir: Path, story_embedder: Optional[Embedder] = None) -> None:
        self.embedder = embedder
        self.story_embedder = story_embedder
        self.db_path = data_dir / "tagger.db"
        self.model_path = data_dir / "classifier.pkl"
        self.db = connect(self.db_path)
        eval_rows = json.loads((BUNDLED / "eval.json").read_text())
        self.eval_titles = {row["title"] for row in eval_rows}
        self.eval_X = embedder.embed([row["title"] for row in eval_rows])
        self.eval_gold = [row["tags"] for row in eval_rows]
        self.classifier: Optional[Classifier] = None

    def start(self) -> None:
        """Stores the seed headlines (embedding any not stored yet) and loads or trains the classifier."""
        seed = json.loads((BUNDLED / "seed.json").read_text())
        self._store([(row["title"], None, None, row["label"]) for row in seed])
        try:
            self.classifier = pickle.loads(self.model_path.read_bytes())
            logger.info("Loaded classifier %s", self.classifier.version)
        except Exception as e:  # Missing, or saved by an incompatible scikit-learn
            logger.info("No usable saved classifier (%s), training one", e)
            self.retrain(force=True)

    def _store(self, items) -> None:
        """Inserts headlines not stored yet; upgrades a stored label from none/"other" to a topic."""
        titles = list(dict.fromkeys(title for title, *_ in items))
        stored = set()
        for start in range(0, len(titles), 500):
            chunk = titles[start:start + 500]
            stored.update(row[0] for row in self.db.execute(
                f"SELECT title FROM headlines WHERE title IN ({','.join('?' * len(chunk))})", chunk))
        new = {title: rest for title, *rest in items if title not in stored}
        if new:
            vectors = self.embedder.embed(list(new))
            now = time.time()
            self.db.executemany(
                "INSERT INTO headlines (title, source, link, label, embedding, first_seen) VALUES (?, ?, ?, ?, ?, ?)",
                [(title, source, link, label, to_blob(v), now) for (title, (source, link, label)), v in zip(new.items(), vectors)],
            )
        self.db.executemany(
            "UPDATE headlines SET label = ? WHERE title = ? AND (label IS NULL OR label = ?)",
            [(label, title, OTHER) for title, _, _, label in items if title in stored and label in TOPICS],
        )
        self.db.commit()

    def tag(self, items: List["Item"]) -> List[List[str]]:
        titles = [clean_title(item.title, item.source) for item in items]
        self._store([(t, item.source, item.link, item.label) for t, item in zip(titles, items)])
        classifier = self.classifier
        rows = {}
        for start in range(0, len(titles), 500):
            chunk = list(dict.fromkeys(titles[start:start + 500]))
            for title, tags, model, blob in self.db.execute(
                f"SELECT title, tags, model, embedding FROM headlines WHERE title IN ({','.join('?' * len(chunk))})", chunk,
            ):
                rows[title] = (tags, model, blob)
        # Tag anything not tagged by the current classifier yet (new, or tagged before a retrain)
        untagged = [title for title, (_, model, _) in rows.items() if model != classifier.version]
        if untagged:
            predicted = classifier.predict(from_blobs([rows[t][2] for t in untagged]))
            self.db.executemany(
                "UPDATE headlines SET tags = ?, model = ? WHERE title = ?",
                [(json.dumps(tags), classifier.version, t) for t, tags in zip(untagged, predicted)],
            )
            self.db.commit()
            for title, tags in zip(untagged, predicted):
                rows[title] = (json.dumps(tags), classifier.version, None)
        return [json.loads(rows[title][0]) for title in titles]


    def retrain(self, force: bool = False) -> None:
        db = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            rows = [(t, label, blob) for t, label, blob in db.execute(
                "SELECT title, label, embedding FROM headlines WHERE label IS NOT NULL ORDER BY first_seen DESC LIMIT ?",
                (TRAIN_MAX_ROWS,)) if t not in self.eval_titles]
        finally:
            db.close()
        candidate = train(from_blobs([blob for *_, blob in rows]), [label for _, label, _ in rows])
        new_score = evaluate(candidate, self.eval_X, self.eval_gold)
        current = self.classifier
        current_score = evaluate(current, self.eval_X, self.eval_gold) if current else None
        if force or current is None or new_score["macro_f1"] >= current_score["macro_f1"]:
            tmp = self.model_path.with_suffix(".tmp")
            tmp.write_bytes(pickle.dumps(candidate))
            tmp.replace(self.model_path)
            self.classifier = candidate
            logger.info("Using classifier %s: %s (previous: %s)", candidate.version, new_score, current_score)
        else:
            logger.warning("Kept classifier %s: %s; candidate %s scored %s",
                           current.version, current_score, candidate.version, new_score)

    def stats(self) -> dict:
        count = self.db.execute("SELECT COUNT(*) FROM headlines").fetchone()[0]
        story_device = self.story_embedder.device if self.story_embedder else None
        return {"device": self.embedder.device, "story_device": story_device, "model": self.classifier.version,
                "headlines": count}


class Item(BaseModel):
    title: str
    source: Optional[str] = None
    link: Optional[str] = None
    label: Optional[str] = None


class TagRequest(BaseModel):
    items: List[Item]


class Verdict(BaseModel):
    a: str
    b: str
    same: bool
    model: Optional[str] = None


class VerdictRequest(BaseModel):
    verdicts: List[Verdict]


_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tagger")


async def on_worker(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(_worker, fn, *args)


async def retrain_periodically(tagger: Tagger) -> None:
    # Counted from the last training (the saved classifier), so restarts don't postpone it
    last = tagger.model_path.stat().st_mtime
    while True:
        await asyncio.sleep(max(0.0, last + RETRAIN_INTERVAL_HOURS * 3600 - time.time()))
        last = time.time()  # Also after a rejected or failed attempt: wait a full interval
        try:
            await asyncio.to_thread(tagger.retrain)
        except Exception:
            logger.exception("Retraining failed, keeping the current classifier")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    started = time.monotonic()

    def setup():
        story = Embedder(STORY_MODEL_DIR, DEVICE, DATA_DIR / "ov_cache", pooling="mean", prefix="query: ")
        tagger = Tagger(Embedder(MODEL_DIR, DEVICE, DATA_DIR / "ov_cache"), DATA_DIR, story)
        tagger.start()
        # The first NPU compile and the training leave ~0.7 GB of freed heap; give it back
        ctypes.CDLL("libc.so.6").malloc_trim(0)
        return tagger

    app.state.tagger = await on_worker(setup)  # The SQLite connection belongs to the worker thread
    logger.info("Tagger ready in %.0fs: %s", time.monotonic() - started, await on_worker(app.state.tagger.stats))
    retraining = asyncio.create_task(retrain_periodically(app.state.tagger))
    yield
    retraining.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.post("/tag")
async def tag(request: TagRequest):
    tagger = app.state.tagger
    tags = await on_worker(tagger.tag, request.items)
    return {"tags": tags, "model": tagger.classifier.version}


@app.post("/group")
async def group(request: TagRequest):
    return {"groups": await on_worker(app.state.tagger.group, request.items)}


@app.get("/story-candidates")
async def story_candidates(limit: int = 20):
    return {"pairs": await on_worker(app.state.tagger.story_candidates, max(1, min(limit, 200)))}


@app.post("/story-verdicts")
async def story_verdicts(request: VerdictRequest):
    return {"stored": await on_worker(app.state.tagger.store_verdicts, request.verdicts)}


@app.get("/health")
async def health():
    return {"status": "ok", **await on_worker(app.state.tagger.stats)}
