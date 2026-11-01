"""
Stress labels from the LLM, for train.py. Run by hand, not in the service:

    python label.py <export.jsonl> <count> <ollama url>

Rates a random sample of the export's headlines (those train.py counts) and adds them to
data/silver.json. It shares the summarizer's Ollama and model, one request at a time, and
steps aside when replies slow down, which means something else is using the GPU.
"""

import json
import random
import statistics
import sys
import time
import urllib.request
from pathlib import Path
from typing import List, Optional

MODEL = "qwen3:8b"
SEED = 1795
BUSY_FACTOR, BUSY_SAMPLE, BUSY_PAUSE = 3, 20, 2
SILVER = Path(__file__).parent / "data/silver.json"
PROMPT = """You rate news headlines. Reply with JSON only: {"stress": N, "countries": ["XX"]}.
stress is how alarming the event itself is, not how dramatic the wording is:
0 = good news, or calm and positive
1 = neutral or routine: politics, business, policy, statements, ordinary local news
2 = worrying: crime, accidents, protests, tension, economic trouble, illness outbreaks
3 = severe: war, armed attacks, mass casualties, major disaster, coup, state of emergency
countries is the ISO 3166 alpha-2 codes of up to 2 countries where the event happens or which it directly affects. Use [] if the headline and outlet do not make it clear."""


def parse_reply(content: str) -> Optional[dict]:
    try:
        reply = json.loads(content)
        stress = int(reply["stress"])
        countries = [str(c).upper() for c in reply.get("countries", [])][:2]
    except (ValueError, KeyError, TypeError):
        return None
    return {"stress": stress, "countries": countries} if 0 <= stress <= 3 else None


def gpu_busy(took: float, recent: List[float]) -> bool:
    return len(recent) >= BUSY_SAMPLE and took > BUSY_FACTOR * statistics.median(recent)


def ask(url: str, source: str, title: str) -> Optional[dict]:
    body = {"model": MODEL, "stream": False, "think": False, "format": "json", "keep_alive": "2m",
            "messages": [{"role": "system", "content": PROMPT},
                         {"role": "user", "content": f"Outlet: {source}\nHeadline: {title}"}],
            "options": {"temperature": 0, "num_predict": 40}}
    request = urllib.request.Request(url + "/api/chat", json.dumps(body).encode(), {"Content-Type": "application/json"})
    return parse_reply(json.load(urllib.request.urlopen(request, timeout=180))["message"]["content"])


def main(export_path: str, count: str, url: str) -> None:
    silver = json.loads(SILVER.read_text())
    known = {row["title"] for row in silver}
    rows = [r for r in map(json.loads, open(export_path, encoding="utf8"))
            if r["title"] not in known and r["tags"] in ('[]', '["uplifting"]')]
    random.Random(SEED).shuffle(rows)
    recent: List[float] = []
    for row in rows[:int(count)]:
        started = time.time()
        reply = ask(url, row["source"], row["title"])
        took = time.time() - started
        if reply:
            silver.append({"title": row["title"], "stress": reply["stress"]})
        if gpu_busy(took, recent[-50:]):
            time.sleep(BUSY_PAUSE)
        recent.append(took)
    SILVER.write_text(json.dumps(silver, ensure_ascii=False, indent=0))
    print(f"{len(silver)} labels")


if __name__ == "__main__":
    main(*sys.argv[1:])
