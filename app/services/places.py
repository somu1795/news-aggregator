"""
Which regions a headline is about, from the places it names.

Shared topic feeds (Mongabay, the Guardian's environment feeds, good-news
outlets) cover the whole world, so without this every region's topic view
shows the same stories. A headline that names a country, its people, or a
major city or state is shown in that place's region (views/topic_views.
build_topic_views); one that names no place stays in every region.

The names are each region's places and abbreviations in regions.yaml (the
site's config folder). A plain list, not a model: headlines name places plainly ("Sierra Leone's
Mahera Coast", "Brazilian Amazon states"), and a list is predictable. Names
that are often something else are left out: Georgia (a US state and a
country), Jordan, Chad, Turkey (turkey vultures), Victoria, Amazon (the
company), English and Polish.
Antarctica, the Arctic and the oceans belong to no region.
"""

import re
from functools import lru_cache
from typing import FrozenSet, Optional, Tuple

from catalog import Catalog, get_catalog


class PlaceMatcher:
    """Finds the regions a text names, from one catalog's places and abbreviations."""

    def __init__(self, catalog: Catalog):
        terms = {" ".join(name.split()): region.id for region in catalog.regions for name in region.places}
        self._exact = {term: region.id for region in catalog.regions for term in region.abbreviations}
        self._by_lower = {t.lower(): r for t, r in terms.items()}
        # Longest names first, so "New South Wales" wins over "Wales" and "Papua New Guinea" over "Guinea"
        self._pattern = re.compile(
            r"(?<![\w-])(" + "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True)) + r")(?![\w-])",
            re.IGNORECASE,
        ) if terms else None
        self._exact_pattern = re.compile(
            r"(?<![\w.])(" + "|".join(re.escape(t) for t in self._exact) + r")(?![\w])"
        ) if self._exact else None
        self.regions_in = lru_cache(maxsize=20000)(self._regions_in)

    def _regions_in(self, title: str) -> FrozenSet[str]:
        """The regions whose places a headline names (empty if none)."""
        found = set()
        if self._pattern:
            found |= {self._by_lower[m.group(1).lower()] for m in self._pattern.finditer(title)}
        if self._exact_pattern:
            found |= {self._exact[m.group(1)] for m in self._exact_pattern.finditer(title)}
        return frozenset(found)

    def item_regions(self, item: dict) -> FrozenSet[str]:
        """A headline's regions; if its title names no place, those its feed text names in its first 300 characters."""
        return self.regions_in(item["title"]) or self.regions_in((item.get("feed_text") or "")[:300])


_last: Optional[Tuple[Catalog, PlaceMatcher]] = None


def matcher_for(catalog: Catalog) -> PlaceMatcher:
    """The catalog's matcher, built once (the patterns hold every place name)."""
    global _last
    if _last is None or _last[0] is not catalog:
        _last = (catalog, PlaceMatcher(catalog))
    return _last[1]


def item_regions(item: dict) -> FrozenSet[str]:
    """A headline's regions, by the site's catalog (see PlaceMatcher.item_regions)."""
    return matcher_for(get_catalog()).item_regions(item)


def regions_in(title: str) -> FrozenSet[str]:
    """The regions whose places a headline names, by the site's catalog (empty if none)."""
    return matcher_for(get_catalog()).regions_in(title)
