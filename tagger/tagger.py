"""
Topic tagger service.

Embeds headlines with bge-small-en-v1.5 (on the Intel NPU via OpenVINO, or the
CPU if no NPU is available) and tags them with a classifier trained on our own
stored headlines.

POST /tag     {"items": [{"title", "source", "link", "label"}]} -> {"tags": [[topic, ...], ...], "model": version}
GET  /health  device, model version, stored headline count

Storage: every headline is stored once in SQLite (DATA_DIR/tagger.db) with its
embedding, the label its feed implies (a Sports section -> "sport", a national
news section -> "other", most feeds -> none) and the model's tags. A headline
is embedded only the first time it is seen.

Training: a logistic regression on the stored embeddings of every labelled
headline (data/seed.json, then everything the feeds add). It is retrained every
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
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import numpy as np
from fastapi import FastAPI
from pydantic import BaseModel
from sklearn.linear_model import LogisticRegression

logger = logging.getLogger("tagger")

TOPICS = ["tech", "science", "nature", "culture", "sport", "uplifting"]
OTHER = "other"  # Label for general news: politics, crime, economy...

MODEL_DIR = Path(os.environ.get("MODEL_DIR", "/models/bge-small-en-v1.5"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DEVICE = os.environ.get("TAGGER_DEVICE", "NPU")
RETRAIN_INTERVAL_HOURS = float(os.environ.get("RETRAIN_INTERVAL_HOURS", "24"))
BUNDLED = Path(__file__).parent / "data"

# NPUs need static input shapes; headlines fit in 48 tokens
BATCH, MAX_TOKENS = 32, 48
# Decision thresholds, tuned on the benchmark (see ARCHITECTURE.md)
TOPIC_THRESHOLD, UPLIFTING_THRESHOLD = 0.5, 0.8


def clean_title(title: str, source: Optional[str] = None) -> str:
    """Drops Google's " - Publisher" suffix and normalizes whitespace (as in the training data)."""
    if source and title.endswith(" - " + source):
        title = title[: -len(source) - 3]
    return re.sub(r"\s+", " ", title).strip()


class Embedder:
    """bge-small via OpenVINO: CLS pooling, L2-normalized, 384 dimensions."""

    def __init__(self, model_dir: Path, device: str, cache_dir: Path) -> None:
        import openvino as ov
        from tokenizers import Tokenizer

        self.tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self.tokenizer.enable_truncation(MAX_TOKENS)
        self.tokenizer.enable_padding(length=MAX_TOKENS)
        core = ov.Core()
        model = core.read_model(model_dir / "model.onnx")
        model.reshape({i.get_any_name(): [BATCH, MAX_TOKENS] for i in model.inputs})
        # The NPU compile takes ~1 minute; CACHE_DIR makes later starts take seconds
        config = {"PERFORMANCE_HINT": "THROUGHPUT", "CACHE_DIR": str(cache_dir)}
        try:
            self.compiled = core.compile_model(model, device, config)
            self.device = device
        except RuntimeError as e:
            if device == "CPU":
                raise
            logger.warning("Could not use %s (%s), falling back to the CPU", device, e)
            self.compiled = core.compile_model(model, "CPU", config)
            self.device = "CPU"

    def embed(self, titles: List[str]) -> np.ndarray:
        out = []
        for start in range(0, len(titles), BATCH):
            batch = titles[start:start + BATCH]
            encoded = self.tokenizer.encode_batch(batch + [""] * (BATCH - len(batch)))
            ids = np.array([e.ids for e in encoded], dtype=np.int64)
            feeds = {
                "input_ids": ids,
                "attention_mask": np.array([e.attention_mask for e in encoded], dtype=np.int64),
                "token_type_ids": np.zeros_like(ids),
            }
            out.append(self.compiled(feeds)[0][: len(batch), 0, :])
        vectors = np.concatenate(out) if out else np.zeros((0, 384), dtype=np.float32)
        return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)


@dataclass
class Classifier:
    version: str
    topic_model: LogisticRegression  # Classes: the topics and "other"
    uplifting_model: LogisticRegression  # Uplifting or not: a tone, so separate from the topic

    def predict(self, X: np.ndarray) -> List[List[str]]:
        classes = list(self.topic_model.classes_)
        topic_probs = self.topic_model.predict_proba(X)
        uplifting_probs = self.uplifting_model.predict_proba(X)[:, 1]
        tags = []
        for probs, uplifting in zip(topic_probs, uplifting_probs):
            row = [c for c, p in zip(classes, probs) if p >= TOPIC_THRESHOLD and c not in (OTHER, "uplifting")]
            if uplifting >= UPLIFTING_THRESHOLD:
                row.append("uplifting")
            tags.append(row)
        return tags


def train(X: np.ndarray, labels: List[str]) -> Classifier:
    def fit(y):
        return LogisticRegression(max_iter=2000, C=4, class_weight="balanced").fit(X, y)

    version = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + f"-n{len(labels)}"
    return Classifier(version, fit(labels), fit([label == "uplifting" for label in labels]))


def evaluate(classifier: Classifier, X: np.ndarray, gold: List[List[str]]) -> dict:
    """Macro F1 over the topics, and the share of untagged (hard news) items that got any tag."""
    predicted = [set(tags) for tags in classifier.predict(X)]
    gold = [set(tags) for tags in gold]
    f1s = []
    for topic in TOPICS:
        tp = sum(topic in p and topic in g for p, g in zip(predicted, gold))
        fp = sum(topic in p and topic not in g for p, g in zip(predicted, gold))
        fn = sum(topic not in p and topic in g for p, g in zip(predicted, gold))
        f1s.append(2 * tp / (2 * tp + fp + fn) if tp else 0.0)
    untagged = [p for p, g in zip(predicted, gold) if not g]
    return {
        "macro_f1": round(sum(f1s) / len(f1s), 4),
        "hard_news_leak": round(sum(bool(p) for p in untagged) / len(untagged), 4) if untagged else 0.0,
    }


SCHEMA = """
CREATE TABLE IF NOT EXISTS headlines (
    title      TEXT PRIMARY KEY,  -- Cleaned title
    source     TEXT,
    link       TEXT,
    label      TEXT,              -- Implied by its feed: a topic, "other", or NULL
    tags       TEXT,              -- JSON list, from classifier `model`
    model      TEXT,
    embedding  BLOB NOT NULL,     -- float16[384]
    first_seen REAL NOT NULL
)
"""


def connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute(SCHEMA)
    return db


def to_blob(vector: np.ndarray) -> bytes:
    return vector.astype(np.float16).tobytes()


def from_blobs(blobs) -> np.ndarray:
    return np.stack([np.frombuffer(b, dtype=np.float16) for b in blobs]).astype(np.float32)


class Tagger:
    """
    All database and embedding work runs on one worker thread (one SQLite
    connection, one NPU request at a time); retraining runs on another thread
    with its own read-only connection and swaps the classifier when done.
    """

    def __init__(self, embedder: Embedder, data_dir: Path) -> None:
        self.embedder = embedder
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
                "SELECT title, label, embedding FROM headlines WHERE label IS NOT NULL") if t not in self.eval_titles]
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
        return {"device": self.embedder.device, "model": self.classifier.version, "headlines": count}


class Item(BaseModel):
    title: str
    source: Optional[str] = None
    link: Optional[str] = None
    label: Optional[str] = None


class TagRequest(BaseModel):
    items: List[Item]


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
        tagger = Tagger(Embedder(MODEL_DIR, DEVICE, DATA_DIR / "ov_cache"), DATA_DIR)
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


@app.get("/health")
async def health():
    return {"status": "ok", **await on_worker(app.state.tagger.stats)}
