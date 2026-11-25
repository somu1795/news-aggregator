"""
The tabs as plain text, for curl and terminals: GET /<view>.txt ("all" or a region id) and
/<view>/<topic>.txt. Numbered headlines, each with its source, domain, age, the other outlets
carrying it, and its link on a line of its own; wrapped for an 80-column terminal.

Read from the worker's in-memory snapshots like /api/headlines, and written as text once per
snapshot (on its first request), so a request costs a dictionary lookup. Cached like the API.

A title reaches the reader's terminal as it is, so control characters are dropped: an escape
sequence in a feed could otherwise recolour the terminal, move its cursor or set its title.
"""

import json
import re
import textwrap
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from catalog import ALL_VIEW, get_catalog, topic_view
from config import settings
from deps import limiter
from routes.frontend import _site_url
from routes.headlines import cache_headers
from services.archive import clean_title
from services.headlines_cache import Snapshot
from site_config import get_site

router = APIRouter()

WIDTH = 80
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")  # C0 but tab and newline, DEL, C1
_SPACES = re.compile(r"\s+")

_texts: Dict[str, Tuple[Snapshot, str]] = {}  # By view: the text of its current snapshot


def _plain(value: Optional[str]) -> str:
    """One line of text with no control characters (whitespace ones become spaces)."""
    return _SPACES.sub(" ", _CONTROL.sub("", _SPACES.sub(" ", value or ""))).strip()


def _age(seconds: float) -> str:
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))}m ago"
    if seconds < 48 * 3600:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def render(view: str, topic: Optional[str], snapshot: dict, site_name: str, site_url: str) -> str:
    """A snapshot as text (see the module). Ages are as of the snapshot, which is at most a refresh old."""
    fetched_at = snapshot["fetched_at"]
    updated = time.strftime("%Y-%m-%d %H:%M", time.gmtime(fetched_at))
    lines = [" · ".join(p for p in (_plain(site_name), view, topic) if p) + f" · updated {updated} UTC", ""]
    items: List[dict] = snapshot["data"]
    number = max(2, len(str(len(items))))
    indent = " " * (number + 2)
    for n, item in enumerate(items, 1):
        source, link = _plain(item["source"]), _plain(item["link"])
        title = textwrap.wrap(_plain(clean_title(item["title"], item["source"])), WIDTH - len(indent)) or [""]
        lines.append(f"{n:>{number}}  {title[0]}")
        lines += [indent + line for line in title[1:]]
        meta = [source, (urlparse(link).hostname or "").removeprefix("www."), _age(fetched_at - item["published"])]
        if item.get("related"):
            meta.append(f"+{len(item['related'])} sources")
        lines.append(indent + " · ".join(p for p in meta if p))
        lines.append(indent + link)
        if item.get("comments") and item["comments"] != item["link"]:
            lines.append(indent + "discuss: " + _plain(item["comments"]))
        lines.append("")
    catalog = get_catalog()
    lines += ["--",
              "tabs:   " + " ".join(f"/{v}.txt" for v in catalog.view_ids),
              "topics: " + " ".join(f"/{view}/{t.id}.txt" for t in catalog.topics),
              f"page:   {site_url}"]
    return "\n".join(textwrap.fill(line, WIDTH, subsequent_indent=" " * 8) if line.startswith(("tabs:", "topics:")) else line
                     for line in lines) + "\n"


async def _serve(request: Request, view: str, topic: Optional[str]) -> PlainTextResponse:
    catalog = get_catalog()
    if view not in catalog.view_ids or topic is not None and topic not in {t.id for t in catalog.topics}:
        return PlainTextResponse(f"No such tab. Try /{ALL_VIEW}.txt\n", status_code=404)
    key = topic_view(view, topic) if topic else view
    snapshot = await request.app.state.headlines_cache.get(key)
    if snapshot is None:
        return PlainTextResponse("Headlines are not available yet, please try again shortly.\n", status_code=503,
                                 headers={"Retry-After": "10"})
    done = _texts.get(key)
    if done is None or done[0] is not snapshot:
        done = (snapshot, render(view, topic, json.loads(snapshot.body), get_site().name, _site_url(request)))
        _texts[key] = done
    return PlainTextResponse(done[1], headers=cache_headers(snapshot))


@router.api_route("/{view}.txt", methods=["GET", "HEAD"], include_in_schema=False)
@limiter.limit(settings.API_RATE_LIMIT)
async def view_text(request: Request, view: str):
    return await _serve(request, view, None)


@router.api_route("/{view}/{topic}.txt", methods=["GET", "HEAD"], include_in_schema=False)
@limiter.limit(settings.API_RATE_LIMIT)
async def topic_text(request: Request, view: str, topic: str):
    return await _serve(request, view, topic)
