"""
Google News URL redirect resolution with Redis-backed caching.

Resolved URLs are cached for 24 hours to avoid repeated round-trips
on subsequent cache misses.
"""

import asyncio
import json
from typing import Optional

import httpx
import redis.asyncio as aioredis
import structlog
from bs4 import BeautifulSoup

from config import settings

logger = structlog.get_logger(__name__)

RESOLVED_URL_PREFIX = "resolved_url:"
RESOLVED_URL_TTL = 86400  # 24 hours

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
)


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
                break

            # 3. Parse the HTML landing page for batchexecute data
            soup = BeautifulSoup(initial_resp.text, "html.parser")
            c_wiz_element = soup.select_one("c-wiz[data-p]")
            if not c_wiz_element:
                logger.warning("Could not find c-wiz element, falling back", url=url, fallback=final_url)
                resolved_url = final_url
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
            else:
                logger.warning("batchexecute parsing failed, falling back to original", url=url)
            break

        except Exception as e:
            logger.warning(
                "Redirect resolution attempt failed",
                url=url, attempt=attempt + 1, error=str(e),
            )
            if attempt < settings.FETCH_RETRIES - 1:
                await asyncio.sleep(settings.BACKOFF_BASE ** attempt)

    # 6. Cache the resolved URL (only if it actually resolved to something different)
    if resolved_url != url:
        try:
            await redis_client.setex(cache_key, RESOLVED_URL_TTL, resolved_url)
        except Exception as e:
            logger.debug("URL cache write failed (non-critical)", url=url, error=str(e))

    return resolved_url
