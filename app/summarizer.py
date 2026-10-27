"""
Background article summarizer.

Runs as its own container (a single replica). Every SUMMARY_INTERVAL_SECONDS it
reads the published snapshots and summarizes the first SUMMARY_TOP_N items of
every view that don't have a summary yet: the main views first, then the topic
views, top positions first. For each, it reads the article's text and asks an
LLM (an Ollama server at SUMMARIZER_LLM_URL) for 2-3 sentences. Summaries are
stored in Redis at summary_key(link) for SUMMARY_TTL_SECONDS, with how the text
was read; the refresher adds them to the items it publishes.

The LLM server may be a machine that's often off: then nothing is fetched or
summarized and the summarizer checks again next interval.

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
from keys import snapshot_key, summary_key
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


async def summarize_new(client: httpx.AsyncClient, redis_client: aioredis.Redis) -> int:
    """One pass over the top items. Returns how many were summarized; raises LLMUnavailable."""
    items = await top_items(redis_client)
    if not items:
        return 0
    known = await redis_client.mget([summary_key(item["link"]) for item in items])
    todo = [item for item, value in zip(items, known) if value is None]
    if not todo:
        return 0
    await check_llm(client)
    reads = Tally()
    done = 0
    for item in todo:
        key = summary_key(item["link"])
        try:
            text, method = await fetch_text(client, redis_client, item["link"])
        except ArticleUnusable as e:
            reads["failed"] += 1
            ARTICLE_READS.labels(method="failed").inc()
            retry = settings.SUMMARY_FAILED_TTL_SECONDS if e.final else settings.SUMMARY_RETRY_SECONDS
            await redis_client.set(key, json.dumps({"failed": str(e)}), ex=retry)
            continue
        reads[method] += 1
        ARTICLE_READS.labels(method=method).inc()
        summary = await summarize(client, item["title"], text)
        if LINK.search(summary):  # Articles can steer the model (prompt injection); a summary never needs a link
            await redis_client.set(key, json.dumps({"failed": "summary contains a link"}), ex=settings.SUMMARY_FAILED_TTL_SECONDS)
            continue
        await redis_client.set(key, json.dumps({"text": summary, "model": settings.SUMMARIZER_MODEL, "via": method}),
                               ex=settings.SUMMARY_TTL_SECONDS)
        done += 1
    logger.info("Summarized articles", summarized=done, pending_before=len(todo), read=dict(reads))
    return done


async def run() -> None:
    configure_logging(settings.LOG_LEVEL)
    logging.getLogger("trafilatura").setLevel(logging.CRITICAL)  # It logs every page it can't parse
    if not settings.SUMMARIZER_LLM_URL:
        logger.info("SUMMARIZER_LLM_URL is not set, summaries are disabled")
        await asyncio.Event().wait()  # Stay up (restart: unless-stopped) without doing anything
    get_catalog(), get_site()  # A broken config folder stops the summarizer at once, with its error
    start_http_server(settings.METRICS_PORT)
    redis_client = create_redis_client(socket_timeout=10, retries=3)
    available: Optional[bool] = None
    async with httpx.AsyncClient(timeout=15.0, http2=True) as client:
        logger.info("Summarizer started", llm=settings.SUMMARIZER_LLM_URL, model=settings.SUMMARIZER_MODEL,
                    browser=settings.SUMMARIZER_BROWSER_URL or None, browser2=settings.SUMMARIZER_BROWSER2_URL or None)
        while True:
            try:
                await summarize_new(client, redis_client)
                await judge_stories(client)  # Summaries come first: only once there are none to make
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
