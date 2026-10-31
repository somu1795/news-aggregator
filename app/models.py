"""
API data models, shared by the refresher (which writes snapshots) and the
headlines routes (which serve them).
"""

from typing import List, Optional

from pydantic import BaseModel, Field


class RelatedItem(BaseModel):
    """Another outlet's headline about the same story."""
    title: str
    link: str
    source: str
    comments: Optional[str] = None  # Its discussion page, when its feed has one (e.g. Hacker News)


class HeadlineItem(BaseModel):
    title: str
    link: str
    source: str
    published: float
    last_updated: float
    comments: Optional[str] = None  # Discussion page, when the feed provides one (e.g. Hacker News)
    summary: Optional[str] = None  # Machine-written summary of the article (top items only, see summarizer.py)
    related: List[RelatedItem] = []  # Other headlines about the same story in this view (services/story_groups.py)
    story: Optional[int] = None  # Its story's id, the same in every view and the archive; None if not grouped


class APIResponse(BaseModel):
    region: str = "all"  # "all" or a region id
    topic: Optional[str] = None  # Set for a topic view
    data: List[HeadlineItem]
    fetched_at: float
    expires_at: float  # When the refresher is next expected to publish


class SearchItem(HeadlineItem):
    views: List[str]  # The views it's in: "all", a region id, or "<view>:<topic>"


class SearchIndex(BaseModel):
    """Every view's items in one response, each link once (GET /api/search-index, for the page's search)."""
    items: List[SearchItem]
    fetched_at: float  # The newest view's
    expires_at: float  # The soonest any view is due to be refreshed


class ArchivedItem(BaseModel):
    """An older story from the archive: its newest matching headline, with its others under it."""
    title: str
    link: str
    source: str
    published: float  # When first seen, for a headline only ever seen folded under another
    summary: Optional[str] = None
    comments: Optional[str] = None
    story: Optional[int] = None  # As in HeadlineItem; None for a headline not given a story yet
    related: List[RelatedItem] = []  # The story's other archived headlines, newest first


class Correction(BaseModel):
    """A typed word nothing starts with, searched as the closest known word."""
    from_: str = Field(alias="from")
    to: str


class ArchiveSearch(BaseModel):
    """Older headlines matching a search (GET /api/search, services/archive_search.py)."""
    items: List[ArchivedItem]
    words: List[str]  # The words searched (corrected ones replaced), for highlighting
    corrected: List[Correction]
    next: Optional[str] = None  # Pass as `after` for the ones after these; None: no more
