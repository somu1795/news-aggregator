"""
Fits model/stress.json and model/relevant.json. Run by hand, not in the service:

    python train.py <export.jsonl>

export.jsonl comes from export_tagger.py. Stress labels are data/silver.json (an LLM's), with the headlines of data/eval.json
(hand-labelled) held out for the score printed here. Relevance is learned from the
tagger's tags, on the feeds the worker doesn't already drop by label: a headline with
no topic, or only "uplifting", is news to count.
"""

import base64
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
SEVERE_WEIGHT = 3  # Severe headlines are few, and the fit would otherwise pull them towards routine
RELEVANT_THRESHOLD = 1.0  # Log-odds: drops 37% of the topic stories left, for 3% more of the news lost


def load(export_path):
    rows = [row for row in map(json.loads, open(export_path, encoding="utf8")) if row["story_embedding"]]  # Not every headline has one
    vectors = np.stack([np.frombuffer(base64.b64decode(r.pop("story_embedding")), dtype=np.float16) for r in rows])
    return rows, vectors.astype(np.float32)


def save(path, weights, intercept, threshold):
    path.write_text(json.dumps({"weights": [round(float(w), 5) for w in weights],
                                "intercept": round(float(intercept), 5), "threshold": threshold}))


def main(export_path):
    rows, vectors = load(export_path)
    silver = {r["title"]: r["stress"] for r in json.load(open(HERE / "data/silver.json", encoding="utf8"))}
    held = {r["title"]: r["stress"] for r in json.load(open(HERE / "data/eval.json", encoding="utf8"))}
    train = [n for n, r in enumerate(rows) if r["title"] in silver and r["title"] not in held]
    test = [n for n, r in enumerate(rows) if r["title"] in held]

    targets = np.array([silver[rows[n]["title"]] for n in train], dtype=float)
    stress = Ridge(alpha=1.0).fit(vectors[train], targets, sample_weight=np.where(targets >= 3, SEVERE_WEIGHT, 1.0))
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
    agreement = (relevant.decision_function(inputs[order[:cut]]) >= RELEVANT_THRESHOLD) == labels[order[:cut]]
    print(f"relevance: {len(pool) - cut} trained, {cut} tested, agreement {agreement.mean():.3f}, "
          f"keeping everything would agree {labels[order[:cut]].mean():.3f}")
    save(HERE / "model/relevant.json", relevant.coef_[0], relevant.intercept_[0], RELEVANT_THRESHOLD)


if __name__ == "__main__":
    main(*sys.argv[1:])
