"""
The tagger's stored headlines with their embeddings, as JSON lines, for backfill.py and
train.py. Run inside the tagger, from the repo's root; it only reads:

    docker compose exec -T tagger python3 - < stats/export_tagger.py > export.jsonl
"""

import base64
import json
import sqlite3
import time

DAYS = 30

db = sqlite3.connect("file:/data/tagger.db?mode=ro", uri=True)
rows = db.execute(
    "SELECT title, source, link, label, tags, first_seen, story_embedding FROM headlines "
    "WHERE first_seen > ? AND story_embedding IS NOT NULL", (time.time() - DAYS * 86400,))
for title, source, link, label, tags, first_seen, embedding in rows:
    print(json.dumps({"title": title, "source": source, "link": link, "label": label, "tags": tags,
                      "first_seen": first_seen, "story_embedding": base64.b64encode(embedding).decode()}))
