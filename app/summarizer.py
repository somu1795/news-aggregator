"""
Background article summarizer.

Runs as its own container (a single replica), as two loops:
- reading: every SUMMARY_INTERVAL_SECONDS it reads the published snapshots and
  reads the text of the first SUMMARY_TOP_N items of every view that have
  neither a summary nor a text yet: the main views first, then the topic views,
  top positions first. Several sites are read at once (SUMMARY_READ_CONCURRENCY),
  each site's articles one at a time. The text is kept for ARTICLE_TEXT_TTL_SECONDS.
- summaries: the read items, in the same order, SUMMARY_BATCH at a time, each
  asked of an LLM (an Ollama server at SUMMARIZER_LLM_URL) for 2-3 sentences.
  Summaries are stored in Redis at summary_key(link) for SUMMARY_TTL_SECONDS,
  with how the text was read; the refresher adds them to the items it publishes.

The LLM server may be a machine that's often off: reading goes on without it,
so when it's back the articles are ready and it summarizes one after another.

An article's text is read, each step only if the previous ones failed:
1. directly (a plain request);
2. with a headless browser (FlareSolverr, at SUMMARIZER_BROWSER_URL), which gets
   through sites' bot checks. Its only way out is a proxy that refuses private
   addresses (Smokescreen), as pages run their own JavaScript. A site that
   blocked a plain request is remembered for BROWSER_HOST_TTL_SECONDS: its
   articles go to the browser first. Once that expires, the next article tries
   a plain request again (and the site is remembered again if it's refused).
   The browser first loads the page without ads, trackers and video players
   (through a second proxy that refuses them, SUMMARIZER_BROWSER_FILTERED_PROXY):
   lighter and faster. If that fails, it loads the whole page ("browser-full"),
   and a site that only works that way is remembered for BROWSER_HOST_TTL_SECONDS;
   If it fails, a second browser with a different engine (Byparr, a modified
   Firefox, at SUMMARIZER_BROWSER2_URL, behind the same proxy) tries: it gets
   past some checks the first doesn't, but is slower;
3. from the text the feed itself carried (stored by the refresher);
4. from the latest archive.ph snapshot.
Paywalled sites (site.yaml paywalled_hosts) go direct, archive, then feed text: a
browser doesn't get past a paywall. Articles none of these can read are marked
and retried after SUMMARY_RETRY_SECONDS.

When nothing waits to be summarized, the same LLM also judges pairs of headlines
that story grouping was unsure of (the tagger's /story-candidates: similar, but
not similar enough to group): same story or not. The verdicts go back to the
tagger (/story-verdicts), and every later grouping follows them, so such stories
come together on a later refresh. Only a yes or no comes back from the LLM.
"""

import asyncio
import json
import logging
import re
from collections import Counter as Tally
from typing import List, Optional

import httpx
import redis.asyncio as aioredis
import structlog
from prometheus_client import Counter, start_http_server

from catalog import get_catalog
from config import settings
from keys import article_text_key, snapshot_key, summary_key
from logging_config import configure_logging
from services.redis_client import create_redis_client
from services.summaries.llm import LLMUnavailable, check_llm, same_story, summarize
from services.summaries.reading import ArticleUnusable, fetch_text
from site_config import get_site

logger = structlog.get_logger(__name__)

LINK = re.compile(r"https?://|www\.", re.IGNORECASE)

ARTICLE_READS = Counter(
    "summarizer_article_reads_total",
    "Articles by how their text was read (direct, browser, browser-full, browser2, feed, archive) or failed",
    ["method"],
)

STORY_JUDGEMENTS = Counter(
    "summarizer_story_judgements_total",
    "Pairs of headlines judged for story grouping, by verdict (same, different)",
    ["verdict"],
)


async def top_items(redis_client: aioredis.Redis) -> List[dict]:
    """The first SUMMARY_TOP_N items of every published view, main views first, by position; unique links."""
    catalog = get_catalog()
    views = catalog.view_ids + catalog.topic_view_ids
    snapshots = await redis_client.mget([snapshot_key(view) for view in views])
    tops = [json.loads(raw)["data"][:settings.SUMMARY_TOP_N] if raw else [] for raw in snapshots]
    main, topics = tops[:len(catalog.view_ids)], tops[len(catalog.view_ids):]
    items = {}
    for group in (main, topics):
        for rank in range(settings.SUMMARY_TOP_N):
            for view_items in group:
                if rank < len(view_items):
                    items.setdefault(view_items[rank]["link"], view_items[rank])
    return list(items.values())


async def judge_stories(client: httpx.AsyncClient) -> int:
    """
    Judges the tagger's unsure pairs, most important first, for up to STORY_JUDGE_SECONDS or
    until none are left. Returns how many were judged; raises LLMUnavailable. If the tagger
    can't be reached, nothing is judged this time.
    """
    loop, judged = asyncio.get_running_loop(), Tally()
    started = loop.time()
    while loop.time() - started < settings.STORY_JUDGE_SECONDS:
        try:
            response = await client.get(f"{settings.TAGGER_URL}/story-candidates",
                                        params={"limit": settings.STORY_JUDGE_BATCH}, timeout=15.0)
            response.raise_for_status()
            pairs = response.json()["pairs"]
            if not pairs:
                break
            verdicts = []
            for pair in pairs:
                same = await same_story(client, pair["a"], pair["b"])
                verdicts.append({"a": pair["a"], "b": pair["b"], "same": same, "model": settings.SUMMARIZER_MODEL})
            response = await client.post(f"{settings.TAGGER_URL}/story-verdicts", json={"verdicts": verdicts}, timeout=15.0)
            response.raise_for_status()
        except httpx.HTTPError as e:  # The tagger, not the LLM (that raises LLMUnavailable)
            logger.warning("Story pairs not judged: tagger unavailable", error=str(e) or type(e).__name__)
            break
        for verdict in verdicts:
            label = "same" if verdict["same"] else "different"
            judged[label] += 1
            STORY_JUDGEMENTS.labels(verdict=label).inc()
    if judged:
        logger.info("Judged story pairs", **judged)
    return sum(judged.values())


async def _unsummarized(redis_client: aioredis.Redis) -> List[tuple]:
    """(item, its read text or None) for the top items without a summary, in top_items' order."""
    items = await top_items(redis_client)
    if not items:
        return []
    links = [item["link"] for item in items]
    # One command: a summary saved (and its text deleted) meanwhile is never seen half done
    found = await redis_client.mget([summary_key(link) for link in links] + [article_text_key(link) for link in links])
    return [(item, read and json.loads(read)) for item, summary, read in zip(items, found[:len(items)], found[len(items):])
            if summary is None]


async def read_ahead(client: httpx.AsyncClient, redis_client: aioredis.Redis) -> int:
    """
    One pass reading the top items' text, LLM or not. Returns how many were read; those that
    can't be are marked, as summaries, and retried later.
    """
    todo = [item["link"] for item, read in await _unsummarized(redis_client) if read is None]
    if not todo:
        return 0
    reads = Tally()
    slots = asyncio.Semaphore(settings.SUMMARY_READ_CONCURRENCY)
    sites = {}  # A site's articles one at a time: the first teaches the next (a bot check, the whole page)

    async def read(link: str) -> None:
        async with sites.setdefault(httpx.URL(link).host, asyncio.Lock()), slots:
            try:
                text, method = await fetch_text(client, redis_client, link)
            except ArticleUnusable as e:
                reads["failed"] += 1
                ARTICLE_READS.labels(method="failed").inc()
                retry = settings.SUMMARY_FAILED_TTL_SECONDS if e.final else settings.SUMMARY_RETRY_SECONDS
                await redis_client.set(summary_key(link), json.dumps({"failed": str(e)}), ex=retry)
                return
            reads[method] += 1
            ARTICLE_READS.labels(method=method).inc()
            await redis_client.set(article_text_key(link), json.dumps({"text": text, "via": method}),
                                   ex=settings.ARTICLE_TEXT_TTL_SECONDS)

    await asyncio.gather(*(read(link) for link in todo))
    read_count = sum(reads.values()) - reads["failed"]
    logger.info("Read articles", read=dict(reads), pending_before=len(todo))
    return read_count


async def summarize_ready(client: httpx.AsyncClient, redis_client: aioredis.Redis) -> int:
    """
    Summarizes up to SUMMARY_BATCH read items, top first. Returns how many were summarized;
    raises LLMUnavailable (asked only when something is ready).
    """
    ready = [(item, read) for item, read in await _unsummarized(redis_client) if read][:settings.SUMMARY_BATCH]
    if not ready:
        return 0
    await check_llm(client)
    done = 0
    for item, read in ready:
        key = summary_key(item["link"])
        summary = await summarize(client, item["title"], read["text"])
        if LINK.search(summary):  # Articles can steer the model (prompt injection); a summary never needs a link
            await redis_client.set(key, json.dumps({"failed": "summary contains a link"}), ex=settings.SUMMARY_FAILED_TTL_SECONDS)
        else:
            await redis_client.set(key, json.dumps({"text": summary, "model": settings.SUMMARIZER_MODEL, "via": read["via"]}),
                                   ex=settings.SUMMARY_TTL_SECONDS)
            done += 1
        await redis_client.delete(article_text_key(item["link"]))
    logger.info("Summarized articles", summarized=done, batch=len(ready))
    return done


async def read_loop(client: httpx.AsyncClient, redis_client: aioredis.Redis) -> None:
    while True:
        try:
            await read_ahead(client, redis_client)
        except Exception as e:
            logger.error("Reading articles failed", error=str(e), exc_info=True)
        await asyncio.sleep(settings.SUMMARY_INTERVAL_SECONDS)


async def summary_loop(client: httpx.AsyncClient, redis_client: aioredis.Redis) -> None:
    available: Optional[bool] = None
    while True:
        done = 0
        try:
            done = await summarize_ready(client, redis_client)
            if not done:  # Summaries come first: only once there are none to make
                done = await judge_stories(client)  # It may take minutes: articles read meanwhile are next
            if available is False:
                logger.info("LLM server is back")
            available = True
        except LLMUnavailable as e:
            if available is not False:  # Log the change, not every retry
                logger.warning("LLM server unavailable, will retry", error=str(e))
            available = False
        except Exception as e:
            logger.error("Summarizer pass failed", error=str(e), exc_info=True)
        if not done:  # After a batch or pairs, the next pass at once (the tabs' order of now)
            await asyncio.sleep(settings.SUMMARY_INTERVAL_SECONDS)


async def run() -> None:
    configure_logging(settings.LOG_LEVEL)
    logging.getLogger("trafilatura").setLevel(logging.CRITICAL)  # It logs every page it can't parse
    if not settings.SUMMARIZER_LLM_URL:
        logger.info("SUMMARIZER_LLM_URL is not set, summaries are disabled")
        await asyncio.Event().wait()  # Stay up (restart: unless-stopped) without doing anything
    get_catalog(), get_site()  # A broken config folder stops the summarizer at once, with its error
    start_http_server(settings.METRICS_PORT)
    redis_client = create_redis_client(socket_timeout=10, retries=3)
    async with httpx.AsyncClient(timeout=15.0, http2=True) as client:
        logger.info("Summarizer started", llm=settings.SUMMARIZER_LLM_URL, model=settings.SUMMARIZER_MODEL,
                    browser=settings.SUMMARIZER_BROWSER_URL or None, browser2=settings.SUMMARIZER_BROWSER2_URL or None)
        await asyncio.gather(read_loop(client, redis_client), summary_loop(client, redis_client))


if __name__ == "__main__":
    asyncio.run(run())
