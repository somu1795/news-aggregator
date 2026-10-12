"""
Fetching URLs that come from outside, e.g. headline links (anyone can post one
to Hacker News): only public internet addresses, checked again at every
redirect. Without this, a link redirecting to a private address (the router,
another LAN or tailnet machine, a container) would have the server fetch it.

A DNS answer that changes between the check and the connection (DNS
rebinding) is not covered; it needs an attacker-controlled DNS server and the
link to reach the front page first.
"""

import asyncio
import ipaddress
import socket
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, List

import httpx

MAX_REDIRECTS = 5


class UnsafeURL(Exception):
    pass


async def _resolve(host: str, port: int) -> List[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [sockaddr[0] for *_, sockaddr in infos]


async def check_public(url: httpx.URL) -> None:
    """Raises UnsafeURL unless the URL is http(s) and every address its host resolves to is public."""
    if url.scheme not in ("http", "https") or not url.host:
        raise UnsafeURL("not an http(s) URL")
    try:
        addresses = await _resolve(url.host, url.port or (443 if url.scheme == "https" else 80))
    except OSError:
        raise UnsafeURL("host not found")
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global:  # Private, loopback, link-local, Tailscale/CGNAT (100.64/10), reserved...
            raise UnsafeURL("not a public address")


@asynccontextmanager
async def stream_public(client: httpx.AsyncClient, url: str, headers: Dict[str, str]) -> AsyncIterator[httpx.Response]:
    """Like client.stream("GET", url, follow_redirects=True), but every hop must be a public address."""
    target = httpx.URL(url)
    for _ in range(MAX_REDIRECTS + 1):
        await check_public(target)
        async with client.stream("GET", target, headers=headers, follow_redirects=False) as response:
            if response.is_redirect:
                target = response.url.join(response.headers["location"])
                continue
            yield response
            return
    raise UnsafeURL("too many redirects")
