"""
Tests for services/safe_fetch.py's client: it connects to public addresses only,
whatever a host name resolves to at that moment, except the services it's told of.
"""

import asyncio

import httpx
import pytest
import pytest_asyncio

from services import safe_fetch

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def local_server():
    """A web server on this machine (a private address); yields (port, the requests it got)."""
    hits = []

    async def handle(reader, writer):
        hits.append(await reader.readline())
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    yield server.sockets[0].getsockname()[1], hits
    server.close()
    await server.wait_closed()


async def test_the_client_never_connects_to_a_private_address(local_server):
    port, hits = local_server
    async with safe_fetch.public_client() as client:
        with pytest.raises(httpx.ConnectError):
            await client.get(f"http://127.0.0.1:{port}/")
    assert hits == []


async def test_a_name_that_resolves_to_a_private_address_is_refused_when_connecting(local_server, monkeypatch):
    # DNS rebinding: the name was public when checked and is private when the connection is made
    port, hits = local_server

    async def resolve(host, port):
        return ["127.0.0.1"]
    monkeypatch.setattr(safe_fetch, "_resolve", resolve)
    async with safe_fetch.public_client() as client:
        with pytest.raises(httpx.ConnectError):
            await client.get(f"http://rebound.example:{port}/")
    assert hits == []


async def test_the_client_reaches_the_internal_services_it_is_told_of(local_server):
    port, hits = local_server
    async with safe_fetch.public_client(internal=[f"http://127.0.0.1:{port}", ""]) as client:
        assert (await client.get(f"http://127.0.0.1:{port}/")).text == "ok"
    assert len(hits) == 1
