"""
What the map shows, from stories. Pure functions; the time is always passed in.

A country's colour is the tone of its stories against the whole world, on fixed
ends, so a calmer or worse world changes the map. Few stories pull it towards the
world's mean and leave it dim: brightness is how much the colour can be trusted,
not how much news there is. A red dot compares a country with its own past, so a
quiet country can fire on a small jump and a big one needs a large jump, and it
needs a stressful story carried by more than one outlet, so a cup final doesn't fire.
"""

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

HALF_LIFE_HOURS = 48
PRIOR = 10  # Stories' worth of the world's mean in every country's score
NO_SIGNAL = 3  # Less evidence than this shows no colour
BLUE, ORANGE = 0.87, 1.39  # Scores at the two ends of the colour scale
WORLD_MEAN_DAYS = 30
NEUTRAL_STRESS = 1.0
DOT_WINDOW_HOURS = 6  # Compared with the same length of time before it, so a dot follows a story within hours
DOT_Z = 2
DOT_MIN_OUTLETS = 2
DOT_STRESS = 1.8
DOT_BASE_DAYS = 14
DOT_MIN_HISTORY_DAYS = 7
DOT_HOLD_HOURS = 24
TOP_STORIES = 5
DAY = 86400.0


@dataclass
class Story:
    id: int
    first_seen: float
    stress: float
    title: str
    link: str
    source: str
    outlets: Set[str]
    countries: Dict[str, bool]  # Country: whether the title names it (else it's the outlet's)
    positions: Dict[str, Tuple[float, float]]  # By country: where its red dot would sit


def weight(story: Story, now: float) -> float:
    return 0.5 ** (max(0.0, now - story.first_seen) / 3600 / HALF_LIFE_HOURS)


def world_mean(stories: List[Story], now: float) -> float:
    recent = [s.stress for s in stories if now - s.first_seen <= WORLD_MEAN_DAYS * DAY]
    return sum(recent) / len(recent) if recent else NEUTRAL_STRESS


def by_country(stories: List[Story]) -> Dict[str, List[Story]]:
    countries: Dict[str, List[Story]] = defaultdict(list)
    for story in stories:
        for country in story.countries:
            countries[country].append(story)
    return countries


def country_scores(stories: List[Story], mean: float, now: float) -> Dict[str, dict]:
    scores = {}
    for country, own in by_country(stories).items():
        evidence = sum(weight(s, now) for s in own)
        score = (sum(weight(s, now) * s.stress for s in own) + PRIOR * mean) / (evidence + PRIOR)
        colour = None if evidence < NO_SIGNAL else min(1.0, max(0.0, (score - BLUE) / (ORANGE - BLUE)))
        scores[country] = {
            "score": score, "colour": colour, "brightness": evidence / (evidence + PRIOR), "evidence": evidence,
            "stories_24h": sum(1 for s in own if now - s.first_seen < DAY),
        }
    return scores


def _hot(story: Story) -> bool:
    return len(story.outlets) >= DOT_MIN_OUTLETS and story.stress >= DOT_STRESS


def blocks_known(earliest: float, now: float) -> int:
    """How many whole past blocks of the baseline there is data for."""
    return min(int((now - earliest) // (DOT_WINDOW_HOURS * 3600)) - 1, DOT_BASE_DAYS * 24 // DOT_WINDOW_HOURS)


def _fires(own: List[Story], blocks: int, now: float) -> Optional[Story]:
    """The story that puts a red dot on a country now, if any."""
    length = DOT_WINDOW_HOURS * 3600
    counts = [0] * (blocks + 1)
    for story in own:
        block = int((now - story.first_seen) // length)
        if 0 <= block <= blocks:
            counts[block] += 1
    past = counts[1:]
    mean = sum(past) / len(past)
    variance = sum((n - mean) ** 2 for n in past) / len(past)
    base = max(mean, 1.0)
    if (counts[0] - base) / math.sqrt(base + variance) < DOT_Z:
        return None
    hot = [s for s in own if now - s.first_seen < length and _hot(s)]
    return max(hot, key=lambda s: (len(s.outlets), s.stress), default=None)


def red_dots(stories: List[Story], held: Dict[str, dict], now: float) -> Dict[str, dict]:
    """The dots now: those that fire, and held ones not yet a day past their last firing."""
    dots = {c: dot for c, dot in held.items() if now - dot["held"] < DOT_HOLD_HOURS * 3600}
    if not stories:
        return dots
    earliest = min(s.first_seen for s in stories)
    if now - earliest < DOT_MIN_HISTORY_DAYS * DAY:
        return dots
    blocks = blocks_known(earliest, now)
    named = defaultdict(list)
    for story in stories:
        for country, from_title in story.countries.items():
            if from_title:
                named[country].append(story)
    for country, own in named.items():
        story = _fires(own, blocks, now)
        if story is not None:
            lat, lon = story.positions.get(country, (None, None))
            dots[country] = {
                "country": country, "lat": lat, "lon": lon, "title": story.title, "link": story.link,
                "outlets": len(story.outlets), "since": held.get(country, {}).get("since", now), "held": now,
            }
    return dots


def _top(own: List[Story], now: float) -> List[dict]:
    ranked = sorted(own, key=lambda s: weight(s, now) * s.stress, reverse=True)
    top = ranked[:TOP_STORIES]
    calm = next((s for s in sorted(ranked[TOP_STORIES:], key=lambda s: weight(s, now), reverse=True)
                 if s.stress < NEUTRAL_STRESS), None)
    return [{"title": s.title, "link": s.link, "source": s.source} for s in top + ([calm] if calm else [])]


def build_output(stories: List[Story], held: Dict[str, dict], now: float) -> dict:
    """heatmap.json. Per-headline stress isn't in it: it's an average's input, not a fact to show."""
    scores = country_scores(stories, world_mean(stories, now), now)
    own = by_country(stories)
    countries = {
        country: {
            "colour": None if s["colour"] is None else round(s["colour"], 3),
            "brightness": round(s["brightness"], 3), "evidence": round(s["evidence"], 1),
            "stories_24h": s["stories_24h"], "top": _top(own[country], now),
        }
        for country, s in scores.items()
    }
    dots = [{k: v for k, v in dot.items() if k != "held"} for dot in held.values()]
    return {"generated_at": now, "half_life_hours": HALF_LIFE_HOURS, "countries": countries, "dots": dots}
