"""
The summarizer's LLM: an Ollama server (SUMMARIZER_LLM_URL) asked, with the
prompts of prompts.yaml, for an article's summary or whether two headlines are
the same story.
"""

import re

import httpx

from config import settings
from site_config import get_site


class LLMUnavailable(Exception):
    pass


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
                    {"role": "system", "content": get_site().prompts.summary},
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


async def same_story(client: httpx.AsyncClient, a: str, b: str) -> bool:
    """The LLM's verdict on two headlines: the same story or not (anything but a yes counts as not)."""
    try:
        response = await client.post(
            f"{settings.SUMMARIZER_LLM_URL}/api/chat",
            json={
                "model": settings.SUMMARIZER_MODEL,
                "messages": [
                    {"role": "system", "content": get_site().prompts.same_story},
                    {"role": "user", "content": f"Headline 1: {a}\nHeadline 2: {b}"},
                ],
                "stream": False,
                "think": False,
                "keep_alive": "2m",
                "options": {"temperature": 0, "num_predict": 3},
            },
            timeout=httpx.Timeout(settings.SUMMARY_LLM_TIMEOUT, connect=5.0),
        )
        response.raise_for_status()
    except httpx.HTTPError as e:
        raise LLMUnavailable(str(e) or type(e).__name__)
    return response.json()["message"]["content"].strip().lower().startswith("yes")
