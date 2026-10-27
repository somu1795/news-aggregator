"""
Google News link resolution: a Google News feed links to news.google.com, not
the publisher; this finds the publisher's URL. Resolved URLs are cached in
Redis for 24 hours.

The custom resolver (resolve_redirect) always runs first, and its result is
used whenever it found the publisher's URL. The fallback (decode_with_signature)
runs only when the custom resolver really failed, i.e. its result is still a
news.google.com URL.

The fallback is deliberately independent of the custom code path: instead of
the `c-wiz[data-p]` payload, it reads the article's signature and timestamp
(`data-n-a-sg` / `data-n-a-ts`) from the article page and builds its own
batchexecute request and response parsing. If Google changes the page in a way
that breaks one method, the other may still work.
"""

import asyncio
import json
import re
from typing import Optional
from urllib.parse import quote

import httpx
import redis.asyncio as aioredis
import structlog
from bs4 import BeautifulSoup

from config import settings
from metrics import REDIRECT_FALLBACKS, REDIRECT_RESOLUTIONS

logger = structlog.get_logger(__name__)

# --- The custom resolver ---

RESOLVED_URL_PREFIX = "resolved_url:"
RESOLVED_URL_TTL = 86400  # 24 hours

_BROWSER_UA = settings.BROWSER_USER_AGENT


def _parse_batch_execute_response(response_text: str) -> Optional[str]:
    """
    Safely parses the complex JSON-like string from Google's batchexecute endpoint.
    """
    try:
        cleaned_text = response_text.replace(")]}'", "")
        outer_array = json.loads(cleaned_text)
        inner_json_string = outer_array[0][2]
        inner_array = json.loads(inner_json_string)
        return inner_array[1]
    except (json.JSONDecodeError, IndexError, TypeError) as e:
        logger.warning(
            "Failed to parse batchexecute response",
            error=str(e),
            snippet=response_text[:200],
        )
        return None


async def resolve_redirect(
    client: httpx.AsyncClient,
    redis_client: aioredis.Redis,
    url: str,
) -> str:
    """
    Resolves a Google News RSS URL. Checks a Redis URL cache first (24h TTL),
    then attempts standard redirect following, then the batchexecute API.
    """
    # 1. Check the URL cache
    cache_key = f"{RESOLVED_URL_PREFIX}{url}"
    try:
        cached = await redis_client.get(cache_key)
        if cached:
            logger.debug("URL cache hit", url=url)
            REDIRECT_RESOLUTIONS.labels(outcome="cache_hit").inc()
            return cached
    except Exception as e:
        logger.debug("URL cache lookup failed (non-critical)", url=url, error=str(e))

    headers = {"user-agent": _BROWSER_UA}
    resolved_url = url  # Default fallback

    for attempt in range(settings.FETCH_RETRIES):
        try:
            # 2. Initial GET, following redirects
            initial_resp = await client.get(
                url, headers=headers, follow_redirects=True, timeout=settings.REQUEST_TIMEOUT,
            )
            initial_resp.raise_for_status()

            final_url = str(initial_resp.url)
            if "news.google.com" not in final_url:
                logger.debug("Resolved via standard redirect", original=url, resolved=final_url)
                resolved_url = final_url
                REDIRECT_RESOLUTIONS.labels(outcome="redirect").inc()
                break

            # 3. Parse the HTML landing page for batchexecute data
            soup = BeautifulSoup(initial_resp.text, "html.parser")
            c_wiz_element = soup.select_one("c-wiz[data-p]")
            if not c_wiz_element:
                logger.warning("Could not find c-wiz element, falling back", url=url, fallback=final_url)
                resolved_url = final_url
                REDIRECT_RESOLUTIONS.labels(outcome="no_cwiz").inc()
                break

            data_p = c_wiz_element.get("data-p")
            obj = json.loads(data_p.replace("%.@.", '[\"garturlreq\",'))

            # 4. POST to batchexecute
            payload = {
                "f.req": json.dumps([[["Fbv4je", json.dumps(obj[:-6] + obj[-2:]), "null", "generic"]]])
            }
            post_headers = {
                "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
                "user-agent": _BROWSER_UA,
            }
            batch_url = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
            response = await client.post(
                batch_url, headers=post_headers, data=payload, timeout=settings.REQUEST_TIMEOUT,
            )
            response.raise_for_status()

            # 5. Extract final URL
            article_url = _parse_batch_execute_response(response.text)
            if article_url:
                logger.debug("Resolved via batchexecute", original=url, resolved=article_url)
                resolved_url = article_url
                REDIRECT_RESOLUTIONS.labels(outcome="batchexecute").inc()
            else:
                logger.warning("batchexecute parsing failed, falling back to original", url=url)
                REDIRECT_RESOLUTIONS.labels(outcome="parse_failed").inc()
            break

        except Exception as e:
            logger.warning(
                "Redirect resolution attempt failed",
                url=url, attempt=attempt + 1, error=str(e),
            )
            if attempt < settings.FETCH_RETRIES - 1:
                await asyncio.sleep(settings.BACKOFF_BASE ** attempt)
    else:
        # No attempt succeeded
        REDIRECT_RESOLUTIONS.labels(outcome="failed").inc()

    # 6. Cache the resolved URL (only if it actually resolved to something different)
    if resolved_url != url:
        try:
            await redis_client.set(cache_key, resolved_url, ex=RESOLVED_URL_TTL)
        except Exception as e:
            logger.debug("URL cache write failed (non-critical)", url=url, error=str(e))

    return resolved_url


# --- The fallback, and the order of the two ---

_ARTICLE_ID = re.compile(r"news\.google\.com/(?:rss/)?articles/([^/?#]+)")
_BATCH_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
_USER_AGENT = settings.BROWSER_USER_AGENT

# The fallback only runs for links the custom resolver failed on; if that's
# every link (Google changed something), don't fire them all at once.
_FALLBACK_SEMAPHORE = asyncio.Semaphore(5)


def _is_unresolved(url: str) -> bool:
    try:
        return httpx.URL(url).host == "news.google.com"
    except httpx.InvalidURL:
        return False


async def decode_with_signature(client: httpx.AsyncClient, url: str) -> Optional[str]:
    """Decodes a Google News article URL via its signature/timestamp. Returns None if it can't."""
    match = _ARTICLE_ID.search(url)
    if not match:
        return None
    article_id = match.group(1)

    async with _FALLBACK_SEMAPHORE:
        page = await client.get(
            f"https://news.google.com/rss/articles/{article_id}",
            headers={"user-agent": _USER_AGENT},
            follow_redirects=True,
            timeout=settings.REQUEST_TIMEOUT,
        )
        page.raise_for_status()
        element = BeautifulSoup(page.text, "html.parser").select_one("c-wiz > div[jscontroller]")
        signature = element.get("data-n-a-sg") if element else None
        timestamp = element.get("data-n-a-ts") if element else None
        if not signature or not timestamp:
            return None

        inner = (
            '["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
            f'"X","X",1,[1,1,1],1,1,null,0,0,null,0],"{article_id}",{timestamp},"{signature}"]'
        )
        response = await client.post(
            _BATCH_URL,
            headers={"content-type": "application/x-www-form-urlencoded;charset=UTF-8", "user-agent": _USER_AGENT},
            content=f"f.req={quote(json.dumps([[['Fbv4je', inner]]]))}",
            timeout=settings.REQUEST_TIMEOUT,
        )
        response.raise_for_status()

    try:
        # Body is ")]}'\n\n<json>"; the first entry's third element is a JSON string ["garturlres", "<url>", ...]
        decoded = json.loads(json.loads(response.text.split("\n\n")[1])[0][2])[1]
    except (IndexError, KeyError, TypeError, ValueError):
        return None
    if isinstance(decoded, str) and decoded.startswith("http") and not _is_unresolved(decoded):
        return decoded
    return None


async def resolve_link(client: httpx.AsyncClient, redis_client: aioredis.Redis, url: str) -> str:
    """Resolves a headline link: custom resolver first, fallback only if it failed."""
    if not _is_unresolved(url):
        return url  # Already a publisher link (e.g. Hacker News); nothing to resolve
    resolved = await resolve_redirect(client, redis_client, url)
    if not _is_unresolved(resolved):
        return resolved

    try:
        fallback = await decode_with_signature(client, url)
    except Exception as e:
        logger.warning("Fallback link resolution errored", url=url, error=str(e))
        fallback = None

    if fallback is None:
        REDIRECT_FALLBACKS.labels(outcome="failed").inc()
        logger.warning("Custom and fallback link resolution both failed", url=url)
        return resolved

    REDIRECT_FALLBACKS.labels(outcome="success").inc()
    # Debug, not info: if the custom resolver breaks, this fires for every link every
    # refresh. Watch redirect_fallbacks_total instead.
    logger.debug("Resolved link via fallback", url=url, resolved=fallback)
    try:
        # Overwrites anything the custom resolver cached (e.g. a Google landing URL)
        await redis_client.set(f"{RESOLVED_URL_PREFIX}{url}", fallback, ex=RESOLVED_URL_TTL)
    except Exception as e:
        logger.debug("URL cache write failed (non-critical)", url=url, error=str(e))
    return fallback
