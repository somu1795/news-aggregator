"""
Background article summarizer.

Runs as its own container (a single replica). Every SUMMARY_INTERVAL_SECONDS it
reads the published snapshots and summarizes the first SUMMARY_TOP_N items of
every view that don't have a summary yet: the main views first, then the topic
views, top positions first. For each, it fetches the article, extracts its text
and asks an LLM (an Ollama server at SUMMARIZER_LLM_URL) for 2-3 sentences.
Summaries are stored in Redis at summary_key(link) for SUMMARY_TTL_SECONDS; the
refresher adds them to the items it publishes.

The LLM server may be a machine that's often off: then nothing is summarized
and the summarizer checks again next interval. Articles that can't be fetched
or have too little text (paywalls, videos) are marked and not retried for
SUMMARY_FAILED_TTL_SECONDS. Paywalled sites' articles are read from an
archive.ph snapshot instead, when there is one (retried every few hours).
"""

import asyncio
import json
import logging
import re
from typing import List, Optional

import httpx
import redis.asyncio as aioredis
import structlog
import trafilatura

from config import CATALOG, is_paywalled, settings, snapshot_key, summary_key
from logging_config import configure_logging
from services.redis_client import create_redis_client
from services.safe_fetch import UnsafeURL, stream_public

logger = structlog.get_logger(__name__)

ARTICLE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
    "Accept-Language": "en-US,en;q=0.5",
}
MAX_ARTICLE_BYTES = 3_000_000
MIN_TEXT_CHARS = 600  # Less is usually a paywall teaser or a video page
MAX_TEXT_CHARS = 6000  # ~1,500 tokens: enough for the gist, keeps prompts fast
LINK = re.compile(r"https?://|www\.", re.IGNORECASE)
ARCHIVE = "https://archive.ph/newest/"  # Redirects to the latest snapshot, or 404s
ARCHIVE_RETRY_SECONDS = 3 * 3600  # Snapshots of new articles often appear later

PROMPT = (
    "You summarize news articles for a headlines site. Write 2-3 plain sentences "
    "(at most 70 words) in English saying what the article reports. Use only facts "
    "stated in the article. No preamble, no opinions, no markdown."
)


class LLMUnavailable(Exception):
    pass


class ArticleUnusable(Exception):
    def __init__(self, reason: str, retry_after: Optional[int] = None):
        super().__init__(reason)
        self.retry_after = retry_after or settings.SUMMARY_FAILED_TTL_SECONDS


async def top_items(redis_client: aioredis.Redis) -> List[dict]:
    """The first SUMMARY_TOP_N items of every published view, main views first, by position; unique links."""
    views = CATALOG.view_ids + CATALOG.topic_view_ids
    snapshots = await redis_client.mget([snapshot_key(view) for view in views])
    tops = [json.loads(raw)["data"][:settings.SUMMARY_TOP_N] if raw else [] for raw in snapshots]
    main, topics = tops[:len(CATALOG.view_ids)], tops[len(CATALOG.view_ids):]
    items = {}
    for group in (main, topics):
        for rank in range(settings.SUMMARY_TOP_N):
            for view_items in group:
                if rank < len(view_items):
                    items.setdefault(view_items[rank]["link"], view_items[rank])
    return list(items.values())


async def fetch_text(client: httpx.AsyncClient, link: str) -> str:
    """
    The article's main text, or ArticleUnusable. A paywalled site's article
    (config.PAYWALLED_HOSTS) that can't be read directly is read from its
    latest archive.ph snapshot, if there is one.
    """
    try:
        return await fetch_page_text(client, link, favor_precision=True)
    except ArticleUnusable as e:
        if not is_paywalled(httpx.URL(link).host):
            raise
        try:
            # archive.ph restructures pages; the default (not precision) extraction keeps the whole article
            return await fetch_page_text(client, ARCHIVE + link, favor_precision=False)
        except ArticleUnusable as archive_error:
            raise ArticleUnusable(f"{e}; archive: {archive_error}", ARCHIVE_RETRY_SECONDS)


async def fetch_page_text(client: httpx.AsyncClient, url: str, favor_precision: bool) -> str:
    try:
        async with stream_public(client, url, ARTICLE_HEADERS) as response:
            if response.status_code != 200:
                raise ArticleUnusable(f"HTTP {response.status_code}")
            if "html" not in response.headers.get("content-type", ""):
                raise ArticleUnusable("not HTML")
            body = b""
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > MAX_ARTICLE_BYTES:
                    raise ArticleUnusable("too large")
    except httpx.HTTPError as e:
        raise ArticleUnusable(type(e).__name__)
    except UnsafeURL as e:
        raise ArticleUnusable(str(e))
    text = await asyncio.to_thread(trafilatura.extract, body, favor_precision=favor_precision)
    if not text or len(text) < MIN_TEXT_CHARS:
        raise ArticleUnusable("too little text")
    return text[:MAX_TEXT_CHARS]


async def summarize(client: httpx.AsyncClient, title: str, text: str) -> str:
    try:
        response = await client.post(
            f"{settings.SUMMARIZER_LLM_URL}/api/chat",
            json={
                "model": settings.SUMMARIZER_MODEL,
                "messages": [
                    {"role": "system", "content": PROMPT},
                    {"role": "user", "content": f"Headline: {title}\n\nArticle:\n{text}"},
                ],
                "stream": False,
                "think": False,
                "keep_alive": "2m",  # Frees the GPU soon after a batch
                "options": {"temperature": 0.2, "num_ctx": 4096, "num_predict": 200},
            },
            timeout=httpx.Timeout(settings.SUMMARY_LLM_TIMEOUT, connect=5.0),
        )
        response.raise_for_status()
    except httpx.HTTPError as e:
        raise LLMUnavailable(str(e) or type(e).__name__)
    summary = re.sub(r"<think>.*?</think>", "", response.json()["message"]["content"], flags=re.S).strip()
    if not summary:
        raise LLMUnavailable("empty response")
    return summary


async def summarize_new(client: httpx.AsyncClient, redis_client: aioredis.Redis) -> int:
    """One pass over the top items. Returns how many were summarized; raises LLMUnavailable."""
    items = await top_items(redis_client)
    if not items:
        return 0
    known = await redis_client.mget([summary_key(item["link"]) for item in items])
    todo = [item for item, value in zip(items, known) if value is None]
    done = 0
    for item in todo:
        key = summary_key(item["link"])
        try:
            text = await fetch_text(client, item["link"])
        except ArticleUnusable as e:
            await redis_client.set(key, json.dumps({"failed": str(e)}), ex=e.retry_after)
            continue
        summary = await summarize(client, item["title"], text)
        if LINK.search(summary):  # Articles can steer the model (prompt injection); a summary never needs a link
            await redis_client.set(key, json.dumps({"failed": "summary contains a link"}), ex=settings.SUMMARY_FAILED_TTL_SECONDS)
            continue
        await redis_client.set(key, json.dumps({"text": summary, "model": settings.SUMMARIZER_MODEL}),
                               ex=settings.SUMMARY_TTL_SECONDS)
        done += 1
    if todo:
        logger.info("Summarized articles", summarized=done, unusable=len(todo) - done, pending_before=len(todo))
    return done


async def run() -> None:
    configure_logging(settings.LOG_LEVEL)
    logging.getLogger("trafilatura").setLevel(logging.CRITICAL)  # It logs every page it can't parse
    if not settings.SUMMARIZER_LLM_URL:
        logger.info("SUMMARIZER_LLM_URL is not set, summaries are disabled")
        await asyncio.Event().wait()  # Stay up (restart: unless-stopped) without doing anything
    redis_client = create_redis_client(socket_timeout=10, retries=3)
    available: Optional[bool] = None
    async with httpx.AsyncClient(timeout=15.0, http2=True) as client:
        logger.info("Summarizer started", llm=settings.SUMMARIZER_LLM_URL, model=settings.SUMMARIZER_MODEL)
        while True:
            try:
                await summarize_new(client, redis_client)
                if available is False:
                    logger.info("LLM server is back")
                available = True
            except LLMUnavailable as e:
                if available is not False:  # Log the change, not every retry
                    logger.warning("LLM server unavailable, will retry", error=str(e))
                available = False
            except Exception as e:
                logger.error("Summarizer pass failed", error=str(e), exc_info=True)
            await asyncio.sleep(settings.SUMMARY_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(run())
