"""
Headlines that aren't news to read, left out of every tab: video and podcast pages (nothing to read, and
nothing to summarize: TSN's video pages read as a list of clip lengths), horoscopes, puzzle hints and answers,
lottery numbers. Measured on the feeds of Nov 13-20, 2026: 358 video pages (Al Jazeera's 212), 170 horoscopes,
84 puzzle answers, about 25 lottery results and 60 podcast episodes, of 43,049 headlines. Articles about videos,
podcasts, puzzles or the lottery, and ones with a clip in them ("Watch: ..."), stay.
"""

import re
from typing import Dict, List
from urllib.parse import urlparse

LISTEN_OR_WATCH = {"video", "videos", "podcast", "podcasts"}  # A whole part of the link's path: /video/..., not /a-video-.../
HOROSCOPE = re.compile(r"\bhoroscopes?\b", re.IGNORECASE)
PUZZLE = re.compile(r"\b(wordle|quordle|octordle|strands|connections|crossword|sudoku|spelling bee|waffle|mini)\b", re.IGNORECASE)
SOLVED = re.compile(r"\b(hints?|answers?|clues?|solutions?)\b", re.IGNORECASE)
# What a daily puzzle's page says that a news story about one doesn't: which day's, or which number
DAILY = re.compile(r"\b(today|nyt|puzzle|no\.|jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|january|february|march|april|"
                   r"june|july|august|september|october|november|december)\b|#\s?\d+", re.IGNORECASE)
LOTTERY = re.compile(r"\b(lotto|powerball|mega millions|euromillions|lottery)\b", re.IGNORECASE)
DRAW = re.compile(r"\b(results?|winning numbers|numbers)\b", re.IGNORECASE)


def unwanted(title: str, link: str) -> bool:
    """Whether a headline isn't news to read (see above)."""
    if LISTEN_OR_WATCH & {part.lower() for part in urlparse(link).path.split("/")}:
        return True
    if HOROSCOPE.search(title):
        return True
    if PUZZLE.search(title) and SOLVED.search(title) and DAILY.search(title):
        return True
    return bool(LOTTERY.search(title) and DRAW.search(title))


def drop_unwanted(views: Dict[str, List[Dict]]) -> Dict[str, List[Dict]]:
    """The views without such headlines: for a Google News item, its real link is only known once resolved."""
    return {view: [h for h in items if not unwanted(h["title"], h["link"])] for view, items in views.items()}
