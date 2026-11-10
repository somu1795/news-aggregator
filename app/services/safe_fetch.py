"""
Fetching URLs that come from outside, e.g. headline links (anyone can post one
to Hacker News): only public internet addresses, checked again at every
redirect. Without this, a link redirecting to a private address (the router,
another LAN or tailnet machine, a container) would have the server fetch it.

The check before a request (check_public) tells an unusable link from a failed
one. What enforces it is the client itself (public_client): it resolves a host
when it connects and connects to that address, so a DNS answer that changes
after the check (DNS rebinding) gets nowhere either.
"""

import asyncio
import ipaddress
import socket
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Iterable, List

import httpcore
import httpx

MAX_REDIRECTS = 5


class UnsafeURL(Exception):
    pass


async def _resolve(host: str, port: int) -> List[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [sockaddr[0] for *_, sockaddr in infos]


async def public_addresses(host: str, port: int) -> List[str]:
    """The addresses a host resolves to, IPv4 first. Raises UnsafeURL unless every one is public."""
    try:
        addresses = await _resolve(host, port)
    except OSError:
        raise UnsafeURL("host not found")
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global:  # Private, loopback, link-local, Tailscale/CGNAT (100.64/10), reserved...
            raise UnsafeURL("not a public address")
    if not addresses:
        raise UnsafeURL("host not found")
    return sorted(addresses, key=lambda address: ":" in address)


async def check_public(url: httpx.URL) -> None:
    """Raises UnsafeURL unless the URL is http(s) and every address its host resolves to is public."""
    if url.scheme not in ("http", "https") or not url.host:
        raise UnsafeURL("not an http(s) URL")
    await public_addresses(url.host, url.port or (443 if url.scheme == "https" else 80))


class _PublicOnly(httpcore.AsyncNetworkBackend):
    """Connects to the address a host resolves to now, if all of its addresses are public."""

    def __init__(self) -> None:
        self._backend = httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        try:
            address = (await public_addresses(host, port))[0]
        except UnsafeURL as e:
            raise httpcore.ConnectError(f"{host}: {e}")
        return await self._backend.connect_tcp(address, port, timeout=timeout, local_address=local_address,
                                               socket_options=socket_options)

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.ConnectError("not a public address")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


def public_client(internal: Iterable[str] = (), limits: httpx.Limits = httpx.Limits(), **kwargs) -> httpx.AsyncClient:
    """
    An HTTP/2 client that connects to public internet addresses only, checked when it connects.
    `internal`: the URLs of our own services (the tagger, the LLM, the browsers; empty ones are
    skipped), whose hosts it reaches as any client would. kwargs are httpx.AsyncClient's.
    """
    transport = httpx.AsyncHTTPTransport(http2=True, limits=limits)
    # httpx has no option for it: the pool it made, with our way of connecting
    transport._pool._network_backend = _PublicOnly()
    mounts = {f"all://{httpx.URL(url).host}": httpx.AsyncHTTPTransport(http2=True, limits=limits) for url in internal if url}
    return httpx.AsyncClient(transport=transport, mounts=mounts, **kwargs)


@asynccontextmanager
async def stream_public(client: httpx.AsyncClient, url: str, headers: Dict[str, str]) -> AsyncIterator[httpx.Response]:
    """Like client.stream("GET", url, follow_redirects=True), but every hop must be a public address."""
    target = httpx.URL(url)
    for _ in range(MAX_REDIRECTS + 1):
        await check_public(target)
        async with client.stream("GET", target, headers=headers, follow_redirects=False) as response:
            if response.has_redirect_location:  # A 3xx without one is returned as it is
                target = response.url.join(response.headers["location"])
                continue
            yield response
            return
    raise UnsafeURL("too many redirects")
