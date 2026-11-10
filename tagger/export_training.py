"""
The rows finetune.py trains the embedding model on, as JSON: data/seed.json's
headlines, then up to PER_LABEL of each label's among the newest 100,000 labelled
headlines in a tagger's database (as retraining takes them), but those of the last
HOLD_OUT_DAYS (kept out, to measure the result on) and data/eval.json's. Run inside
the tagger, from the repo's root; it only reads:

    docker compose exec -T tagger python3 - < tagger/export_training.py > training.json

Release tagger-model-v2's training.json was made this way on Nov 10, 2026, and
also without 625 tech-section headlines an LLM judged misfiled (a cricket feature
in a Technology section); leaving them in made no measurable difference.
"""

import json
import random
import sqlite3

PER_LABEL, HOLD_OUT_DAYS, SEED = 12000, 2, 0

db = sqlite3.connect("file:/data/tagger.db?mode=ro", uri=True)
rows = db.execute("SELECT title, label, first_seen FROM headlines WHERE label IS NOT NULL "
                  "ORDER BY first_seen DESC LIMIT 100000").fetchall()
cutoff = rows[0][2] - HOLD_OUT_DAYS * 86400
excluded = {r["title"] for r in json.load(open("/tagger/data/eval.json"))}
seed = [r for r in json.load(open("/tagger/data/seed.json")) if r["title"] not in excluded]
by_label = {}
for title, label, first_seen in rows:
    if first_seen < cutoff and title not in excluded:
        by_label.setdefault(label, []).append({"title": title, "label": label})
rng = random.Random(SEED)
sampled = [row for items in by_label.values() for row in rng.sample(items, min(PER_LABEL, len(items)))]
print(json.dumps(seed + sampled, ensure_ascii=False))
