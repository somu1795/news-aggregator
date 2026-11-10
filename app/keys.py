"""
Redis keys shared by the services: one function per kind of key, so a key's
format is written once.
"""
import hashlib

from config import settings


def snapshot_key(view: str) -> str:
    """Redis key holding the latest snapshot of a view ("all", a region id, or a topic_view())."""
    return f"{settings.HEADLINES_KEY}:{view}"


def summary_key(link: str) -> str:
    """Redis key holding an article's summary (or a failure marker), by its link."""
    return "summary:" + hashlib.sha256(link.encode()).hexdigest()[:32]


def subject_key(link: str) -> str:
    """Redis key holding what a headline is about, as the summarizer's LLM named it, by its link."""
    return "subject:" + hashlib.sha256(link.encode()).hexdigest()[:32]


def article_text_key(link: str) -> str:
    """Redis key holding an article's text, read ahead of its summary, and how it was read, by its link."""
    return "article-text:" + hashlib.sha256(link.encode()).hexdigest()[:32]


def feed_text_key(link: str) -> str:
    """Redis key holding the text the feed itself carried for an article, by its link."""
    return "feed-text:" + hashlib.sha256(link.encode()).hexdigest()[:32]


def browser_host_key(host: str) -> str:
    """Redis key marking a site that blocks plain requests (its articles are read with the browser)."""
    return f"browser-host:{host}"


def full_page_host_key(host: str) -> str:
    """Redis key marking a site whose articles the browser can't read without ads and trackers."""
    return f"full-page-host:{host}"
