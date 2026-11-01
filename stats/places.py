"""
The countries a headline is about: the first two its title names (countries,
their people, leaders, big cities and states, from data/places.json, built by
build_places.py), or else the country of its outlet (data/homes.json).
Names are matched as written, so "jordan shoes" is not Jordan. Names that are
often something else are left out of the data.
"""

import html
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

MAX_COUNTRIES = 2


def clean_title(title: str) -> str:
    """A feed's title as text: entities decoded, spaces tidied."""
    return " ".join(html.unescape(title).split())


@dataclass
class Hit:
    country: str
    lat: Optional[float]  # A named city's position; None for a country, person or state
    lon: Optional[float]


@dataclass
class Located:
    countries: List[str]
    from_title: bool  # False: from the outlet's country
    lat: Optional[float]  # The city named, or else the first country's capital
    lon: Optional[float]


class Places:
    def __init__(self, terms: Dict[str, list], abbreviations: Dict[str, str],
                 capitals: Dict[str, list], homes: Dict[str, str]):
        self.terms, self.abbreviations, self.capitals, self.homes = terms, abbreviations, capitals, homes
        # Longest first, so "Papua New Guinea" wins over "Guinea"
        names = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
        self._names = re.compile(r"(?<![\w-])(" + names + r")(?![\w-])")
        short = "|".join(re.escape(t) for t in abbreviations)
        self._short = re.compile(r"(?<![\w.])(" + short + r")(?![\w])")

    @classmethod
    def load(cls, data_dir: Path) -> "Places":
        data = json.loads((data_dir / "places.json").read_text())
        homes = json.loads((data_dir / "homes.json").read_text())
        return cls(data["terms"], data["abbreviations"], data["capitals"], homes)

    def detect(self, title: str) -> List[Hit]:
        """The first countries a title names, in order, each once."""
        found: List[Tuple[int, Hit]] = [(m.start(), Hit(*self.terms[m.group(1)])) for m in self._names.finditer(title)]
        found += [(m.start(), Hit(self.abbreviations[m.group(1)], None, None)) for m in self._short.finditer(title)]
        hits: List[Hit] = []
        for _, hit in sorted(found, key=lambda pair: pair[0]):
            seen = next((h for h in hits if h.country == hit.country), None)
            if seen is None:
                hits.append(hit)
            elif seen.lat is None:
                seen.lat, seen.lon = hit.lat, hit.lon
        return hits[:MAX_COUNTRIES]

    def locate(self, title: str, source: str) -> Located:
        hits = self.detect(title)
        from_title = bool(hits)
        countries = [hit.country for hit in hits]
        if not hits and source in self.homes:
            countries = [self.homes[source]]
        city = next((hit for hit in hits if hit.lat is not None), None)
        if city is not None:
            return Located(countries, from_title, city.lat, city.lon)
        lat, lon = self.capitals.get(countries[0], (None, None)) if countries else (None, None)
        return Located(countries, from_title, lat, lon)
