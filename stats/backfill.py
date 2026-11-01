"""
Seeds a new database with stored headlines, so the map and the red dots have a
past from their first day. Run once, by hand, with the worker stopped:

    python backfill.py <export.jsonl>

Each line of the export, from the tagger's database, has title, source, link, label,
first_seen and story_embedding (float16[384], base64). Headlines already stored are skipped.
"""

import base64
import json
import sys

import numpy as np

from worker import build_state, ingest_rows

BATCH = 2000


def vector(encoded: str) -> np.ndarray:
    v = np.frombuffer(base64.b64decode(encoded), dtype=np.float16).astype(np.float32)
    return v / np.linalg.norm(v)


def main(path: str) -> None:
    state = build_state(None)
    rows = [json.loads(line) for line in open(path, encoding="utf8")]
    rows.sort(key=lambda r: r["first_seen"])
    counted = 0
    for start in range(0, len(rows), BATCH):
        batch = rows[start:start + BATCH]
        for row in batch:
            row["vector"] = vector(row["story_embedding"])
        counted += ingest_rows(batch, state)
    print(f"{len(rows)} headlines read, {counted} counted")


if __name__ == "__main__":
    main(*sys.argv[1:])
