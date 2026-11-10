"""
At most SUBJECT_CAP headlines about one subject per topic tab.

Story grouping folds the same news from several outlets into one item, but a
subject in the news for days brings many different stories about it (GTA 6:
its release times, its console versions, its maker's results), and they would
fill a tab. The summarizer has its LLM name each topic tab headline's subject
in a few words ("GTA 6", "Russell Brand", "COP31"), stored at subject_key(link).
In each tab the first SUBJECT_CAP headlines about a subject keep their places
and the later ones leave it, so the stories after them move up. They are still
archived (searchable). A headline not named yet (the LLM was off) is never capped.
"""

import json
import re
from typing import Dict, List, Tuple

import redis.asyncio as aioredis

from config import settings
from keys import subject_key

ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10"}


def normalize_subject(name: str) -> str:
    """Case, punctuation and a leading "the" ignored; a roman numeral after a word as digits ("GTA VI" = "GTA 6")."""
    words = re.sub(r"[^a-z0-9]+", " ", name.lower()).split()
    if words[:1] == ["the"]:
        words = words[1:]
    return " ".join(ROMAN.get(w, w) if i else w for i, w in enumerate(words))


async def subjects_of(redis_client: aioredis.Redis, views: Dict[str, List[Dict]]) -> Dict[str, str]:
    """{link: normalized subject} of the views' items that have one."""
    links = list({item["link"] for items in views.values() for item in items})
    if not links:
        return {}
    found = await redis_client.mget([subject_key(link) for link in links])
    named = {link: normalize_subject(json.loads(raw)["subject"]) for link, raw in zip(links, found) if raw}
    return {link: subject for link, subject in named.items() if subject}


def cap_subjects(views: Dict[str, List[Dict]], subject_of: Dict[str, str]) -> Tuple[Dict[str, List[Dict]], List[Dict]]:
    """Each view without the headlines past the SUBJECT_CAP-th about one subject; and those (once each)."""
    capped, dropped = {}, {}
    for view, items in views.items():
        count: Dict[str, int] = {}
        kept = []
        for item in items:
            subject = subject_of.get(item["link"])
            if subject:
                count[subject] = count.get(subject, 0) + 1
                if count[subject] > settings.SUBJECT_CAP:
                    dropped.setdefault(item["link"], item)
                    continue
            kept.append(item)
        capped[view] = kept
    kept_links = {item["link"] for items in capped.values() for item in items}
    return capped, [item for link, item in dropped.items() if link not in kept_links]
