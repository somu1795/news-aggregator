"""
Parsing a fetched feed into headlines: title, link, publisher, date, its
discussion page and the article text the feed itself carries.
"""

import calendar
import re
import time
from typing import Dict, List
from urllib.parse import urlparse

import feedparser
import lxml.etree
import lxml.html

from config import settings


def feed_text(entry) -> str:
    """
    The article text the feed itself carries (its full content, or a teaser), as
    plain text. The summarizer falls back on it when the article page can't be read.
    """
    html = max([c.get("value") or "" for c in entry.get("content", [])] + [entry.get("summary") or ""], key=len)
    if not html.strip():
        return ""
    try:
        text = lxml.html.fromstring(html).text_content()
    except (ValueError, lxml.etree.ParserError):
        return ""
    return re.sub(r"\s+", " ", text).strip()[:settings.SUMMARY_MAX_TEXT_CHARS]


def discussion_link(link: str, comments: str | None) -> str | None:
    """The feed's comments URL, unless it's just the article itself (e.g. its "#comments" anchor)."""
    if not comments or urlparse(comments)._replace(fragment="").geturl() == urlparse(link)._replace(fragment="").geturl():
        return None
    return comments


IMAGE = re.compile(r"\.(jpe?g|png|gif|webp|avif)$", re.IGNORECASE)


def publisher(entry, source_name: str) -> str:
    """
    The item's own <source> (Google News: the article's publisher), else the feed's name from the
    config. A <source> that is a photo credit (France 24 puts "© Fred Tanneau, AFP" there, pointing
    at the photo) is not a publisher.
    """
    source = getattr(entry, "source", {})
    title = (source.get("title") or "").strip()
    if not title or title.startswith("\u00a9") or IMAGE.search(urlparse(source.get("href") or "").path):
        return source_name
    return title


def parse(content: bytes, source_name: str) -> List[Dict]:
    feed = feedparser.parse(content)
    # Only treat bozo (malformed XML) as fatal if no entries were parsed.
    # Many real-world feeds are technically malformed but usable.
    if getattr(feed, "bozo", False) and not feed.entries:
        raise ValueError(f"Feed parse error for {source_name}: {feed.bozo_exception}")
    headlines = []
    for entry in feed.entries:
        if not entry.get("title") or not entry.get("link"):
            continue  # One broken item mustn't cost the whole feed
        headlines.append({
            "title": entry.title,
            "link": entry.link,
            "source": publisher(entry, source_name),
            "published": (
                calendar.timegm(entry.published_parsed)
                if getattr(entry, "published_parsed", None)
                else time.time()
            ),
            "last_updated": time.time(),
            "comments": discussion_link(entry.link, entry.get("comments")),
            "feed_text": feed_text(entry),  # Not published (the API's model has no such field)
        })
    return headlines
