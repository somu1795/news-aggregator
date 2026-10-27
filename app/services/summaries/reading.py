"""
Reading an article's text, each way only if the ones before failed (see
fetch_text): a plain request, the headless browser (without ads and trackers,
then the whole page), the second browser, the feed's own text, an archive.ph
snapshot. Paywalled sites skip the browsers.
"""

import asyncio
from typing import Tuple

import httpx
import redis.asyncio as aioredis
import structlog
import trafilatura

from config import settings
from keys import browser_host_key, feed_text_key, full_page_host_key
from services.safe_fetch import UnsafeURL, check_public, stream_public
from site_config import get_site

logger = structlog.get_logger(__name__)

ARTICLE_HEADERS = {
    "User-Agent": settings.BROWSER_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
    "Accept-Language": "en-US,en;q=0.5",
}
MAX_ARTICLE_BYTES = 3_000_000
ARCHIVE = "https://archive.ph/newest/"  # Redirects to the latest snapshot, or 404s
BLOCKING_STATUSES = {401, 402, 403, 429, 503}  # What bot checks answer a plain request with
BOT_CHECK_HEADERS = ("cf-mitigated", "x-datadome", "x-amzn-waf-action")  # Cloudflare, DataDome, AWS WAF (HTTP 202)


class ArticleUnusable(Exception):
    def __init__(self, reason: str, blocked: bool = False, final: bool = False):
        super().__init__(reason)
        self.blocked = blocked  # The site refused a plain request (a bot check)
        self.final = final  # Nothing may read it another way (not a public address)


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

    # Without ads and trackers first, unless the site is known to need them
    filtered = (bool(settings.SUMMARIZER_BROWSER_FILTERED_PROXY)
                and not await redis_client.exists(full_page_host_key(host)))

    async def from_browser_filtered() -> str:
        return await fetch_browser_text(client, settings.SUMMARIZER_BROWSER_URL, link,
                                        settings.SUMMARIZER_BROWSER_FILTERED_PROXY)

    async def from_browser() -> str:
        text = await fetch_browser_text(client, settings.SUMMARIZER_BROWSER_URL, link,
                                        settings.SUMMARIZER_BROWSER_PROXY)
        if filtered:  # It failed without them: this site needs the whole page
            await redis_client.set(full_page_host_key(host), 1, ex=settings.BROWSER_HOST_TTL_SECONDS)
            logger.info("Site needs the whole page in the browser", host=host)
        return text

    async def from_browser2() -> str:
        return await fetch_browser_text(client, settings.SUMMARIZER_BROWSER2_URL, link,
                                        settings.SUMMARIZER_BROWSER_PROXY)

    async def from_feed() -> str:
        text = await redis_client.get(feed_text_key(link))
        if not text:
            raise ArticleUnusable("none")
        return text if isinstance(text, str) else text.decode()

    async def from_archive() -> str:
        # archive.ph restructures pages; the default (not precision) extraction keeps the whole article
        return await fetch_page_text(client, ARCHIVE + link, favor_precision=False)

    if get_site().is_paywalled(host):
        steps = [("archive", from_archive), ("feed", from_feed)]
    else:
        if not browser:
            browser_steps = []
        elif filtered:
            browser_steps = [("browser", from_browser_filtered), ("browser-full", from_browser)]
        else:
            # A site known to need the whole page, or no filtered proxy
            whole = "browser-full" if settings.SUMMARIZER_BROWSER_FILTERED_PROXY else "browser"
            browser_steps = [(whole, from_browser)]
        steps = (browser_steps
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


async def fetch_browser_text(client: httpx.AsyncClient, browser_url: str, link: str, proxy: str) -> str:
    """The article's text as a real browser (FlareSolverr's API) at browser_url sees it, through proxy (public-only)."""
    try:
        await check_public(httpx.URL(link))
    except UnsafeURL as e:
        raise ArticleUnusable(str(e), final=True)
    request = {"cmd": "request.get", "url": link, "maxTimeout": int(settings.SUMMARY_BROWSER_TIMEOUT * 1000)}
    if proxy:
        request["proxy"] = {"url": proxy}
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
