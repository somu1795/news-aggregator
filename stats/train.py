"""
Fits model/stress.json and model/relevant.json. Run by hand, not in the service:

    python train.py <headlines.jsonl> <embeddings.bin>

headlines.jsonl has one {"id", "title", "tags", "label"} per line, from the tagger's database;
embeddings.bin is one record per headline: its id (int64), its story_embedding (float16[384]).
Stress labels are data/silver.json (an LLM's), with the headlines of data/eval.json
(hand-labelled) held out for the score printed here. Relevance is learned from the
tagger's tags, on the feeds the worker doesn't already drop by label: a headline with
no topic, or only "uplifting", is news to count.
"""

import json
import sys
from pathlib import Path

import numpy as np
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.metrics import f1_score

HERE = Path(__file__).parent
WORRYING = 1.5  # Stress at or above this is "worrying or worse"
COUNTED = ('[]', '["uplifting"]')
UNDROPPED_LABELS = (None, "other", "uplifting")


def load(headlines_path, embeddings_path):
    record = np.dtype([("id", "<i8"), ("vector", "<f2", 384)])
    stored = np.fromfile(embeddings_path, dtype=record)
    row_of = {int(i): n for n, i in enumerate(stored["id"])}
    rows = [r for r in map(json.loads, open(headlines_path, encoding="utf8")) if r["id"] in row_of]
    vectors = stored["vector"][[row_of[r["id"]] for r in rows]].astype(np.float32)
    return rows, vectors


def save(path, weights, intercept, threshold):
    path.write_text(json.dumps({"weights": [round(float(w), 5) for w in weights],
                                "intercept": round(float(intercept), 5), "threshold": threshold}))


def main(headlines_path, embeddings_path):
    rows, vectors = load(headlines_path, embeddings_path)
    silver = {r["title"]: r["stress"] for r in json.load(open(HERE / "data/silver.json", encoding="utf8"))}
    held = {r["title"]: r["stress"] for r in json.load(open(HERE / "data/eval.json", encoding="utf8"))}
    train = [n for n, r in enumerate(rows) if r["title"] in silver and r["title"] not in held]
    test = [n for n, r in enumerate(rows) if r["title"] in held]

    stress = Ridge(alpha=1.0).fit(vectors[train], [silver[rows[n]["title"]] for n in train])
    predicted, truth = stress.predict(vectors[test]), np.array([held[rows[n]["title"]] for n in test])
    print(f"stress: {len(train)} trained, {len(test)} tested, "
          f"F1 {f1_score(truth >= 2, predicted >= WORRYING):.3f}, "
          f"percentiles 5/50/95 {np.percentile(stress.predict(vectors), [5, 50, 95]).round(2)}")
    save(HERE / "model/stress.json", stress.coef_, stress.intercept_, 0.0)

    pool = [n for n, r in enumerate(rows) if r.get("label") in UNDROPPED_LABELS]
    labels = np.array([rows[n]["tags"] in COUNTED for n in pool])
    order = np.random.default_rng(1795).permutation(len(pool))
    cut = len(pool) // 5
    inputs = vectors[pool]
    relevant = LogisticRegression(max_iter=4000).fit(inputs[order[cut:]], labels[order[cut:]])
    agreement = (relevant.decision_function(inputs[order[:cut]]) >= 0) == labels[order[:cut]]
    print(f"relevance: {len(pool) - cut} trained, {cut} tested, agreement {agreement.mean():.3f}, "
          f"keeping everything would agree {labels[order[:cut]].mean():.3f}")
    save(HERE / "model/relevant.json", relevant.coef_[0], relevant.intercept_[0], 0.0)


if __name__ == "__main__":
    main(*sys.argv[1:])
