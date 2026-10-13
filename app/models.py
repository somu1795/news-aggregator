"""
API data models, shared by the refresher (which writes snapshots) and the
headlines route (which serves them).
"""

from typing import List, Optional

from pydantic import BaseModel


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


class APIResponse(BaseModel):
    region: str = "all"  # "all" or a region id
    topic: Optional[str] = None  # Set for a topic view
    data: List[HeadlineItem]
    fetched_at: float
    expires_at: float  # When the refresher is next expected to publish
