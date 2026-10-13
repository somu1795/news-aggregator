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
   a plain request again (and the site is remembered again if it's refused);
   If it fails, a second browser with a different engine (Byparr, a modified
   Firefox, at SUMMARIZER_BROWSER2_URL, behind the same proxy) tries: it gets
   past some checks the first doesn't, but is slower;
3. from the text the feed itself carried (stored by the refresher);
4. from the latest archive.ph snapshot.
Paywalled sites (config.PAYWALLED_HOSTS) go direct, archive, then feed text: a
browser doesn't get past a paywall. Articles none of these can read are marked
and retried after SUMMARY_RETRY_SECONDS.
"""

import asyncio
import json
import logging
import re
from collections import Counter as Tally
from typing import List, Optional, Tuple

import httpx
import redis.asyncio as aioredis
import structlog
import trafilatura
from prometheus_client import Counter, start_http_server

from config import CATALOG, browser_host_key, feed_text_key, is_paywalled, settings, snapshot_key, summary_key
from logging_config import configure_logging
from services.redis_client import create_redis_client
from services.safe_fetch import UnsafeURL, check_public, stream_public

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
LINK = re.compile(r"https?://|www\.", re.IGNORECASE)
ARCHIVE = "https://archive.ph/newest/"  # Redirects to the latest snapshot, or 404s
BLOCKING_STATUSES = {401, 402, 403, 429, 503}  # What bot checks answer a plain request with
BOT_CHECK_HEADERS = ("cf-mitigated", "x-datadome", "x-amzn-waf-action")  # Cloudflare, DataDome, AWS WAF (HTTP 202)

ARTICLE_READS = Counter(
    "summarizer_article_reads_total",
    "Articles by how their text was read (direct, browser, browser2, feed, archive) or failed",
    ["method"],
)

PROMPT = (
    "You summarize news articles for a headlines site. Write 2-3 plain sentences "
    "(at most 70 words) in English saying what the article reports. Use only facts "
    "stated in the article. No preamble, no opinions, no markdown."
)


class LLMUnavailable(Exception):
    pass


class ArticleUnusable(Exception):
    def __init__(self, reason: str, blocked: bool = False, final: bool = False):
        super().__init__(reason)
        self.blocked = blocked  # The site refused a plain request (a bot check)
        self.final = final  # Nothing may read it another way (not a public address)


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


async def fetch_text(client: httpx.AsyncClient, redis_client: aioredis.Redis, link: str) -> Tuple[str, str]:
    """(The article's main text, how it was read), or ArticleUnusable. See the module docstring for the order."""
    host = httpx.URL(link).host
    browser = bool(settings.SUMMARIZER_BROWSER_URL)
    errors = []
    if not (browser and await redis_client.exists(browser_host_key(host))):
        try:
            return await fetch_page_text(client, link, favor_precision=True), "direct"
        except ArticleUnusable as e:
            if e.final:
                raise
            if e.blocked and browser:
                await redis_client.set(browser_host_key(host), 1, ex=settings.BROWSER_HOST_TTL_SECONDS)
            errors.append(str(e))

    async def from_browser() -> str:
        return await fetch_browser_text(client, settings.SUMMARIZER_BROWSER_URL, link)

    async def from_browser2() -> str:
        return await fetch_browser_text(client, settings.SUMMARIZER_BROWSER2_URL, link)

    async def from_feed() -> str:
        text = await redis_client.get(feed_text_key(link))
        if not text:
            raise ArticleUnusable("none")
        return text if isinstance(text, str) else text.decode()

    async def from_archive() -> str:
        # archive.ph restructures pages; the default (not precision) extraction keeps the whole article
        return await fetch_page_text(client, ARCHIVE + link, favor_precision=False)

    if is_paywalled(host):
        steps = [("archive", from_archive), ("feed", from_feed)]
    else:
        steps = (([("browser", from_browser)] if browser else [])
                 + ([("browser2", from_browser2)] if settings.SUMMARIZER_BROWSER2_URL else [])
                 + [("feed", from_feed), ("archive", from_archive)])
    for method, step in steps:
        try:
            return await step(), method
        except ArticleUnusable as e:
            if e.final:
                raise
            errors.append(f"{method}: {e}")
    raise ArticleUnusable("; ".join(errors))


async def fetch_page_text(client: httpx.AsyncClient, url: str, favor_precision: bool) -> str:
    try:
        async with stream_public(client, url, ARTICLE_HEADERS) as response:
            if response.status_code != 200:
                blocked = (response.status_code in BLOCKING_STATUSES
                           or any(header in response.headers for header in BOT_CHECK_HEADERS))
                raise ArticleUnusable(f"HTTP {response.status_code}", blocked=blocked)
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
        raise ArticleUnusable(str(e), final=True)
    return await extract_text(body, favor_precision)


async def fetch_browser_text(client: httpx.AsyncClient, browser_url: str, link: str) -> str:
    """The article's text as a real browser (FlareSolverr's API) at browser_url sees it, through the public-only proxy."""
    try:
        await check_public(httpx.URL(link))
    except UnsafeURL as e:
        raise ArticleUnusable(str(e), final=True)
    request = {"cmd": "request.get", "url": link, "maxTimeout": int(settings.SUMMARY_BROWSER_TIMEOUT * 1000)}
    if settings.SUMMARIZER_BROWSER_PROXY:
        request["proxy"] = {"url": settings.SUMMARIZER_BROWSER_PROXY}
    try:
        response = await client.post(f"{browser_url}/v1", json=request,
                                     timeout=settings.SUMMARY_BROWSER_TIMEOUT + 30)
        result = response.json()
    except (httpx.HTTPError, ValueError) as e:
        raise ArticleUnusable(type(e).__name__)
    solution = result.get("solution") or {}
    if result.get("status") != "ok":
        raise ArticleUnusable(str(result.get("message") or "failed")[:120])
    if solution.get("status") != 200:
        raise ArticleUnusable(f"HTTP {solution.get('status')}")
    return await extract_text(str(solution.get("response") or "").encode(), favor_precision=True)


async def extract_text(html: bytes, favor_precision: bool) -> str:
    """The page's main text. Precise extraction drops some sites' whole article, so too little is retried without it."""
    text = await asyncio.to_thread(trafilatura.extract, html, favor_precision=favor_precision)
    if favor_precision and (not text or len(text) < settings.SUMMARY_MIN_TEXT_CHARS):
        text = await asyncio.to_thread(trafilatura.extract, html, favor_precision=False)
    if not text or len(text) < settings.SUMMARY_MIN_TEXT_CHARS:
        raise ArticleUnusable("too little text")
    return text[:settings.SUMMARY_MAX_TEXT_CHARS]


async def check_llm(client: httpx.AsyncClient) -> None:
    """Raises LLMUnavailable unless the LLM server answers, so nothing is fetched for nothing."""
    try:
        response = await client.get(f"{settings.SUMMARIZER_LLM_URL}/api/tags", timeout=httpx.Timeout(10.0, connect=5.0))
        response.raise_for_status()
    except httpx.HTTPError as e:
        raise LLMUnavailable(str(e) or type(e).__name__)


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
    start_http_server(settings.METRICS_PORT)
    redis_client = create_redis_client(socket_timeout=10, retries=3)
    available: Optional[bool] = None
    async with httpx.AsyncClient(timeout=15.0, http2=True) as client:
        logger.info("Summarizer started", llm=settings.SUMMARIZER_LLM_URL, model=settings.SUMMARIZER_MODEL,
                    browser=settings.SUMMARIZER_BROWSER_URL or None, browser2=settings.SUMMARIZER_BROWSER2_URL or None)
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
